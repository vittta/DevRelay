"""User project registration, independent of task authorization and execution."""

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import tomllib
from urllib.parse import quote
import uuid

from . import __version__
from .config import ensure_ignores, ignore_additions, initial_configuration, initialize, validate
from .errors import RelayError
from .git import common_dir, has_ref, repository_root, worktrees
from .state import State


def default_catalog():
    directory = Path(os.environ.get("XDG_CONFIG_HOME", "")).expanduser()
    if not directory.is_absolute():
        directory = Path.home() / ".config"
    return directory / "devrelay/projects.toml"


def runtime_info():
    import devrelay
    package = Path(devrelay.__file__).resolve().parent
    result = {"version": __version__, "path": str(package), "python": sys.executable}
    try:
        distribution = importlib.metadata.distribution("devrelay")
        if Path(distribution.locate_file("")).resolve() == package.parent:
            direct = json.loads(distribution.read_text("direct_url.json") or "{}")
            commit = direct.get("vcs_info", {}).get("commit_id")
            if commit:
                result["commit"] = commit
    except (importlib.metadata.PackageNotFoundError, ValueError):
        pass
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def repository_state(root):
    """Read registration without initializing a database or clearing process markers."""
    path = common_dir(root) / "devrelay/state.sqlite3"
    if not path.exists():
        return {}, []
    try:
        connection = sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True, timeout=10)
        try:
            metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key, data FROM metadata")}
            tasks = [json.loads(row[0]) for row in connection.execute("SELECT data FROM tasks ORDER BY id")]
            return metadata, tasks
        finally:
            connection.close()
    except (sqlite3.Error, ValueError) as error:
        raise RelayError(f"Cannot read repository state; preserved: {error}") from error


def registered_controller(root):
    metadata, _ = repository_state(root)
    saved = metadata.get("controller_root")
    if not isinstance(saved, str) or not saved:
        return None
    path = Path(saved)
    if not path.is_dir():
        return None
    registered = {Path(item["worktree"]).resolve() for item in worktrees(root)}
    if path.resolve() not in registered or common_dir(path) != common_dir(root):
        return None
    return path.resolve()


def _common(path):
    try:
        return str(common_dir(repository_root(path)))
    except (RelayError, OSError):
        return None


class Registry:
    def __init__(self, path=None):
        self.path = (Path(path) if path is not None else default_catalog()).expanduser().resolve()

    def read(self):
        try:
            raw = self.path.read_bytes() if self.path.exists() else b""
            data = tomllib.loads(raw.decode()) if raw else {"projects": []}
        except (OSError, ValueError) as error:
            raise RelayError(f"Cannot read project catalog {self.path}; preserved: {error}") from error
        if set(data) != {"projects"} or not isinstance(data["projects"], list):
            raise RelayError("Catalog must contain [[projects]] entries or projects = []")
        ids = set()
        for entry in data["projects"]:
            if not isinstance(entry, dict) or set(entry) - {"id", "name", "path", "runtime", "repository_id"}:
                raise RelayError("Unknown project catalog fields")
            identifier = entry.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", identifier) or identifier in ids:
                raise RelayError("Project IDs must be unique simple names")
            ids.add(identifier)
            if not isinstance(entry.get("path"), str) or not entry["path"]:
                raise RelayError("Every project needs a path")
            if any(not isinstance(entry[key], str) or not entry[key] for key in ("name", "runtime") if key in entry):
                raise RelayError("Project name/runtime must be nonempty strings")
            if "repository_id" in entry and not re.fullmatch(r"[a-f0-9]{32}", str(entry["repository_id"])):
                raise RelayError("Invalid repository identity in project catalog")
        return raw, data["projects"]

    def path_for(self, entry, key="path"):
        return (self.path.parent / entry[key]).resolve()

    @contextmanager
    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RelayError("Another project registration is in progress; retry") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def write(self, entries, before):
        current, _ = self.read()
        if current != before:
            raise RelayError("Project catalog changed; check the project again")
        if before:
            directory = self.path.parent / "catalog-backups"
            directory.mkdir(mode=0o700, exist_ok=True)
            backup = directory / (hashlib.sha256(before).hexdigest() + ".toml")
            if not backup.exists():
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(before)
        text = "\n\n".join("[[projects]]\n" + "\n".join(
            f"{key} = {json.dumps(entry[key], ensure_ascii=False)}"
            for key in ("id", "name", "path", "runtime", "repository_id") if key in entry)
            for entry in entries) + "\n" if entries else "projects = []\n"
        descriptor, temporary = tempfile.mkstemp(prefix=".projects-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def plan(self, path, *, name="", replace_id=None):
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise RelayError("Enter a local Git repository path / 请填写本机 Git 仓库路径")
        if not isinstance(name, str) or len(name) > 120 or any(ord(c) < 32 for c in name):
            raise RelayError("Project name must be at most 120 characters")
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_dir():
                raise RelayError(f"Repository directory does not exist / 仓库目录不存在：{path}")
        except (ValueError, RuntimeError) as error:
            raise RelayError("Invalid repository path / 仓库路径无效，请检查后重试") from error
        root = repository_root(candidate)
        if not has_ref(root, "HEAD"):
            raise RelayError("Repository needs an initial commit / 仓库需要至少一个提交")
        common = str(common_dir(root))
        metadata, tasks = repository_state(root)
        identity = metadata.get("project_identity")
        if identity is not None and (not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity)):
            raise RelayError("Invalid saved repository identity; state preserved")
        raw, entries = self.read()
        matches = []
        for entry in entries:
            entry_common = _common(self.path_for(entry))
            saved_identity = entry.get("repository_id")
            if saved_identity and entry_common == common and saved_identity != identity:
                raise RelayError("Registered repository identity changed; restore its original state before adding it")
            if saved_identity and saved_identity == identity:
                if entry_common is not None and entry_common != common:
                    raise RelayError("Repository identity is already used by another checkout; copied state preserved")
                matches.append(entry)
            elif not saved_identity and entry_common == common:
                matches.append(entry)
        if len(matches) > 1:
            raise RelayError("This Git repository appears more than once in the catalog; remove the duplicate entries")
        if replace_id is not None:
            entry = next((entry for entry in entries if entry["id"] == replace_id), None)
            if entry is None or entry not in matches:
                raise RelayError("Repository identity does not match; existing project preserved / 仓库不匹配，原项目已保留")
        else:
            entry = matches[0] if matches else None
        controller = registered_controller(root)
        target = root if replace_id is not None or controller is None else controller
        previous = metadata.get("controller_root")
        if previous is not None and (not isinstance(previous, str) or not Path(previous).is_absolute()):
            raise RelayError("Invalid saved controller path; state preserved")
        relocating = bool(previous and Path(previous).resolve() != target)
        if tasks and (relocating or controller is None):
            raise RelayError("Repository has saved tasks; restore the original controller before relocating / 请先恢复原目录，保留任务与证据")
        config_path = target / ".devrelay/config.toml"
        if config_path.is_symlink() or config_path.parent.is_symlink():
            raise RelayError("Configuration must be a regular file, not a symlink; preserved")
        existing = config_path.exists()
        if existing:
            source = config_path.read_text()
            notes = []
        else:
            source, discovery = initial_configuration(target)
            notes = discovery["notes"]
        try:
            config = validate(target, tomllib.loads(source))
        except ValueError as error:
            raise RelayError(f"Invalid project configuration; preserved: {error}") from error
        if relocating and controller is not None:
            original = controller / ".devrelay/config.toml"
            if not original.is_file() or original.read_text() != source:
                raise RelayError("Controller configurations differ; preserve and reconcile them before relocating")
        ignore, additions = ignore_additions(target)
        identifier = entry["id"] if entry else ((re.sub(r"[^a-zA-Z0-9_-]+", "-", target.name).strip("-") or "project")
                                                + "-" + _digest(common)[:8])
        if not entry and any(item["id"] == identifier for item in entries):
            raise RelayError("Project ID is already in use; existing entry preserved")
        result = {"id": identifier, "name": name.strip() or (entry or {}).get("name") or target.name,
                  "path": str(root), "root": str(target), "config_path": str(config_path),
                  "existing_config": existing, "already_added": entry is not None,
                  "relocating": relocating, "previous_controller": previous,
                  "provider": config.provider, "base": config.base, "source": source,
                  "checks": config.data["checks"], "setup": config.data["setup"],
                  "ignore_additions": additions, "notes": notes,
                  "repository": config.repo, "replace_id": replace_id,
                  "queue_mode": config.data["queue"]["mode"],
                  "auto_triage": config.data["discovery"]["auto_triage"],
                  "auto_merge": config.data["delivery"]["auto_merge"]}
        result["revision"] = _digest({"proposal": result, "common": common,
                                      "state": [metadata, tasks], "ignore": ignore,
                                      "catalog": raw.hex()})
        return result

    def add(self, path, *, name="", replace_id=None, revision=None):
        with self.lock():
            proposal = self.plan(path, name=name, replace_id=replace_id)
            if revision is not None and revision != proposal["revision"]:
                raise RelayError("Project or catalog changed; check the project again / 项目已变化，请重新检查")
            state = State(Path(proposal["root"]))
            try:
                with state.lock():
                    current = self.plan(path, name=name, replace_id=replace_id)
                    if current["revision"] != proposal["revision"]:
                        raise RelayError("Repository state changed; check the project again")
                    raw, entries = self.read()
                    root = Path(proposal["root"])
                    if not proposal["existing_config"]:
                        initialize(root, expected_source=proposal["source"])
                    else:
                        ensure_ignores(root)
                    identity = state.metadata("project_identity") or uuid.uuid4().hex
                    state.set_metadata("project_identity", identity)
                    state.set_metadata("controller_root", str(root))
                    entry = next((item for item in entries if item["id"] == proposal["id"]), None)
                    updated = {**(entry or {}), "id": proposal["id"], "name": proposal["name"],
                               "path": str(root), "repository_id": identity}
                    if entry != updated:
                        entries = [updated if item["id"] == proposal["id"] else item for item in entries]
                        if entry is None:
                            entries.append(updated)
                        self.write(entries, raw)
                    return {"id": proposal["id"], "root": str(root), "catalog": str(self.path),
                            "created_config": not proposal["existing_config"], "already_added": proposal["already_added"],
                            "needs_acceptance": not proposal["checks"], "github_setup": proposal["provider"] == "github"}
            finally:
                state.close()
