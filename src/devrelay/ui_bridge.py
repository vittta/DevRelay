"""Isolated dashboard adapter, executed with the selected runtime on sys.path.

The server selects this adapter's project and runtime; configuration requests
cannot override either. Registration manages repository paths separately. The
selected runtime owns validation, controller routing and locks.
"""

import difflib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib


def digest(data):
    return hashlib.sha256(data).hexdigest()


def runtime_digest(directory):
    parts = [path.name.encode() + b"\0" + path.read_bytes() for path in sorted(directory.glob("*.py"))]
    return digest(b"\0".join(parts))


def main(request, root, runtime):
    sys.path.insert(0, str(runtime))
    import devrelay
    from devrelay import config as configuration
    from devrelay.cli import controller
    from devrelay.errors import RelayError
    from devrelay.git import repository_root
    from devrelay.state import State, group_alive

    # Do not silently fall back to the dashboard's version when a catalog path
    # is wrong or an older runtime is missing the supported validation API.
    package = Path(devrelay.__file__).resolve().parent
    if package != (runtime / "devrelay").resolve() or not callable(getattr(configuration, "validate", None)):
        raise ValueError("Selected runtime does not expose config.validate; update it before using the dashboard")
    runtime_sha = runtime_digest(package)
    state = State(repository_root(root))
    try:
        root = controller(repository_root(root), state)
        path = root / ".devrelay/config.toml"

        def read():
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                raise ValueError("Configuration must be an existing regular file, not a symlink")
            return path.read_bytes()

        def revision(raw):
            return digest(str(root).encode() + b"\0" + runtime_sha.encode() + b"\0" + raw)

        raw = read()
        source = raw.decode("utf-8")

        def validate(text):
            return configuration.validate(root, tomllib.loads(text))

        def activity():
            # Reading status must not clear active-process metadata.
            busy, reason = False, "No DevRelay lock; external agent activity is not observable"
            with (state.directory / "worker.lock").open("a+") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    busy, reason = True, "A DevRelay worker or command holds the repository lock"
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
            if state.metadata("workspace_owner"):
                busy, reason = True, "Repository is reserved by a multi-repository task"
            active = state.metadata("active_process")
            if active and group_alive(active["group"]):
                busy, reason = True, "A saved command process group is still running"
            return {"busy": busy, "reason": reason}

        operation = request["operation"]
        if operation == "inspect":
            errors, config, explicit = [], None, {}
            try:
                explicit = tomllib.loads(source)
                config = validate(source)
            except (RelayError, ValueError) as error:
                errors.append(str(error))
            defaults = configuration.validate(root, {}).data
            supported = [f"{section}.{key}" for section, fields in defaults.items()
                         if isinstance(fields, dict) for key in fields]
            for section, key, value in (("project", "repository", "owner/repo"),
                                        ("codex", "model", "example"), ("codex", "profile", "example")):
                try:
                    configuration.validate(root, {section: {key: value}})
                except RelayError:
                    continue
                supported.append(f"{section}.{key}")
            try:
                status = activity()
            except (RelayError, OSError) as error:
                status = {"busy": True, "reason": str(error)}
            tasks = [{key: task.get(key) for key in ("id", "title", "status", "pr_url", "updated_at")}
                     for task in state.tasks()]
            return {"source": source, "revision": revision(raw), "data": config.data if config else {},
                    "explicit": explicit, "defaults": defaults, "supported": supported,
                    "errors": errors, "root": str(root), "config_path": str(path),
                    "runtime": {"version": devrelay.__version__, "path": str(package), "sha256": runtime_sha, "python": sys.executable},
                    "activity": status, "tasks": tasks}

        if operation == "setup-github":
            if request.get("revision") != revision(raw):
                raise ValueError("Configuration changed; reload before initializing GitHub")
            from devrelay.onboarding import setup_github
            with state.lock():
                if read() != raw:
                    raise ValueError("Configuration changed; reload before initializing GitHub")
                return setup_github(validate(source))

        if operation in ("queue", "doctor"):
            config = validate(source)
            if operation == "doctor":
                from devrelay.cli import doctor
                return doctor(config)
            from devrelay.providers import queue_report, snapshot
            report = queue_report(config, snapshot(config, state))
            # Keep issue bodies and model payloads out of the dashboard API.
            return {"eligible": [{"id": item["id"], "title": item.get("title", "")}
                                 for item in report["eligible"]],
                    "excluded": report["excluded"],
                    "triage_candidates": [{"id": item["id"], "title": item.get("title", "")}
                                          for item in report.get("triage_candidates", [])]}

        if operation not in ("preview", "save"):
            raise ValueError("Unknown dashboard operation")
        if request.get("revision") != revision(raw):
            raise ValueError("Configuration or runtime changed; reload before editing (配置已变化，请重新加载)")
        if "source" in request:
            candidate = request["source"]
            if not isinstance(candidate, str):
                raise ValueError("source must be TOML text")
        else:
            candidate = source
        if "changes" in request:
            changes = request["changes"]
            if not isinstance(changes, dict):
                raise ValueError("changes must be an object")
            spec = importlib.util.spec_from_file_location("dashboard_document", Path(__file__).with_name("ui_document.py"))
            document = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(document)
            if changes:
                candidate = document.update(candidate, changes)
        updated = validate(candidate)
        try:
            original = validate(source)
        except (RelayError, ValueError):
            original = None
        stages = ("setup", "checks", "review", "delivery")
        if original and hasattr(original, "stage_fingerprint"):
            affected = [stage for stage in stages if original.stage_fingerprint(stage) != updated.stage_fingerprint(stage)]
        else:
            affected = list(stages)
        preview = {"source": candidate, "data": updated.data, "explicit": tomllib.loads(candidate),
                   "revision": revision(raw),
                   "preview_sha256": digest(revision(raw).encode() + candidate.encode()),
                   "diff": "".join(difflib.unified_diff(source.splitlines(keepends=True),
                                                       candidate.splitlines(keepends=True),
                                                       fromfile="config.toml (current)", tofile="config.toml (proposed)")),
                   "affected_stages": affected, "changed": candidate != source,
                   "activity": activity()}
        if operation == "preview":
            return preview
        if request.get("preview_sha256") != preview["preview_sha256"]:
            raise ValueError("Preview the exact changes before saving")
        with state.lock():
            if read() != raw or runtime_digest(package) != runtime_sha:
                raise ValueError("Configuration or runtime changed; reload before saving")
            if not preview["changed"]:
                return {"saved": False, "revision": revision(raw)}
            backup_dir = state.directory / "ui-backups"
            if backup_dir.is_symlink():
                raise ValueError("Backup directory must not be a symlink")
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            backup = backup_dir / (digest(raw) + ".toml")
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if backup.is_symlink() or backup.read_bytes() != raw:
                    raise ValueError("Existing backup differs from the original configuration")
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(candidate.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
                if read() != raw:
                    raise ValueError("Configuration changed while saving; original edits were preserved")
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {"saved": True, "backup": str(backup), "revision": revision(candidate.encode()),
                "affected_stages": affected}
    finally:
        state.close()


if __name__ == "__main__":
    try:
        response = main(json.load(sys.stdin), Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps({"result": response}, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
