"""Validated project configuration and conservative command discovery."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib

from .errors import RelayError
from .git import git, github_slug, has_ref, remote_tracking_ref


@dataclass(frozen=True)
class Config:
    root: Path
    data: dict
    fingerprint: str

    @property
    def provider(self):
        return self.data["project"]["provider"]

    @property
    def base(self):
        return self.data["project"]["base_branch"]

    @property
    def remote(self):
        return self.data["project"]["remote"]

    @property
    def repo(self):
        return self.data["project"].get("repository", "")

    @property
    def prefix(self):
        return self.data["project"]["branch_prefix"]

    @property
    def base_ref(self):
        if self.provider != "github":
            return "refs/heads/" + self.base
        ref = remote_tracking_ref(self.root, self.remote, self.base)
        if ref is None:
            raise RelayError(f"Base {self.base} has no fetch tracking mapping for remote {self.remote}; "
                             "configure a positive fetch refspec before using this project")
        return ref


def _table(data, name):
    value = data.setdefault(name, {})
    if not isinstance(value, dict):
        raise RelayError(f"[{name}] must be a table")
    return value


def _keys(table, allowed, name):
    unexpected = set(table) - set(allowed)
    if unexpected:
        raise RelayError(f"Unknown {name} field(s): {', '.join(sorted(unexpected))}")


def load(root):
    path = root / ".devrelay/config.toml"
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RelayError(f"Cannot read {path}; run devrelay init: {error}") from error
    return validate(root, data)


def validate(root, data):
    """Validate explicit configuration without running discovery or writing files."""
    _keys(data, ("project", "queue", "codex", "delivery", "setup", "checks"), "configuration")
    project = _table(data, "project")
    _keys(project, ("provider", "base_branch", "remote", "branch_prefix", "repository"), "project")
    for key, default in {"provider": "local", "base_branch": "main", "remote": "origin",
                         "branch_prefix": "devrelay/task-"}.items():
        project.setdefault(key, default)
    if project["provider"] not in ("local", "github"):
        raise RelayError("project.provider must be local or github")
    for key in ("base_branch", "remote", "branch_prefix"):
        value = project[key]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value):
            raise RelayError(f"Invalid project.{key}")
    if project["provider"] == "github" and (not isinstance(project.get("repository"), str)
                                            or not re.fullmatch(r"[\w.-]+/[\w.-]+", project["repository"])):
        raise RelayError("project.repository must be owner/repo for GitHub tasks")
    git(root, "check-ref-format", "--branch", project["base_branch"])
    git(root, "check-ref-format", "--branch", project["branch_prefix"] + "example")
    queue = _table(data, "queue")
    _keys(queue, ("approval_label", "priorities", "blockers"), "queue")
    queue.setdefault("approval_label", "auto:approved")
    queue.setdefault("priorities", ["priority:P0", "priority:P1", "priority:P2"])
    queue.setdefault("blockers", ["blocked", "needs-input"])
    for key in ("priorities", "blockers"):
        if (not isinstance(queue[key], list) or not queue[key]
                or any(not isinstance(item, str) or not item for item in queue[key])
                or len(set(queue[key])) != len(queue[key])):
            raise RelayError(f"queue.{key} must contain unique nonempty strings")
    if not isinstance(queue["approval_label"], str) or not queue["approval_label"]:
        raise RelayError("queue.approval_label must be a nonempty string")
    codex = _table(data, "codex")
    _keys(codex, ("executable", "sandbox", "timeout_seconds", "max_attempts", "model", "profile"), "codex")
    codex.setdefault("executable", "codex")
    codex.setdefault("sandbox", "workspace-write")
    codex.setdefault("timeout_seconds", 1800)
    codex.setdefault("max_attempts", 2)
    if codex["sandbox"] not in ("read-only", "workspace-write", "danger-full-access"):
        raise RelayError("Invalid codex.sandbox")
    for key in ("executable", "model", "profile"):
        if key in codex and (not isinstance(codex[key], str) or not codex[key].strip()):
            raise RelayError(f"codex.{key} must be a nonempty string")
    for table, key, default in ((codex, "timeout_seconds", 1800), (codex, "max_attempts", 2)):
        value = table.get(key, default)
        if type(value) is not int or value <= 0:
            raise RelayError(f"codex.{key} must be a positive integer")
    delivery = _table(data, "delivery")
    _keys(delivery, ("auto_review", "auto_merge", "required_checks", "allow_no_ci", "cleanup_branches", "cleanup_worktrees"), "delivery")
    for key in ("auto_review", "auto_merge", "allow_no_ci", "cleanup_branches", "cleanup_worktrees"):
        delivery.setdefault(key, False)
        if type(delivery[key]) is not bool:
            raise RelayError(f"delivery.{key} must be a boolean")
    delivery.setdefault("required_checks", [])
    required = delivery["required_checks"]
    if (not isinstance(required, list) or any(not isinstance(item, str) or not item.strip() for item in required)
            or len(set(required)) != len(required)):
        raise RelayError("delivery.required_checks must contain unique nonempty check names")
    if project["provider"] != "github" and (delivery["auto_review"] or delivery["auto_merge"]):
        raise RelayError("Automatic PR review/merge requires project.provider=github")
    if delivery["cleanup_branches"] and not delivery["cleanup_worktrees"]:
        raise RelayError("delivery.cleanup_branches requires cleanup_worktrees=true to release the checked-out branch")
    if project["provider"] != "github" and (delivery["cleanup_branches"] or delivery["cleanup_worktrees"]):
        raise RelayError("Post-merge cleanup requires project.provider=github")
    for phase in ("setup", "checks"):
        entries = data.setdefault(phase, [])
        if not isinstance(entries, list):
            raise RelayError(f"{phase} must be an array of tables")
        ids = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise RelayError(f"Every {phase} entry must be a table")
            _keys(entry, ("id", "argv", "timeout_seconds"), phase)
            identifier, argv = entry.get("id"), entry.get("argv")
            if (not isinstance(identifier, str) or not re.fullmatch(r"[\w-]+", identifier)
                    or identifier in ids):
                raise RelayError(f"Every {phase} command needs a unique simple id")
            ids.add(identifier)
            if (not isinstance(argv, list) or not argv
                    or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)):
                raise RelayError(f"{phase}.{identifier}.argv must be an argument array")
            entry.setdefault("timeout_seconds", 600)
            if type(entry["timeout_seconds"]) is not int or entry["timeout_seconds"] <= 0:
                raise RelayError(f"{phase}.{identifier}.timeout_seconds must be positive")
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return Config(root, data, fingerprint)


def detect(root):
    setup, checks, notes = [], [], []
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text()).get("scripts", {})
        except (ValueError, OSError) as error:
            raise RelayError(f"Cannot inspect package.json: {error}") from error
        manager = ("pnpm" if (root / "pnpm-lock.yaml").exists() else
                   "yarn" if (root / "yarn.lock").exists() else
                   "bun" if any((root / name).exists() for name in ("bun.lock", "bun.lockb")) else "npm")
        installs = {"npm": ["npm", "ci"] if (root / "package-lock.json").exists() else ["npm", "install"],
                    "pnpm": ["pnpm", "install", "--frozen-lockfile"],
                    "yarn": ["yarn", "install"], "bun": ["bun", "install", "--frozen-lockfile"]}
        setup.append({"id": "install", "argv": installs[manager]})
        for name in ("lint", "typecheck", "test", "build"):
            if name in scripts and not (name == "test" and "no test specified" in scripts[name]):
                checks.append({"id": name, "argv": [manager, "run", name]})
        notes.append("Review test scripts for watch mode and project-specific environment needs.")
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        setup.append({"id": "venv", "argv": ["python3", "-m", "venv", ".venv"]})
        setup.append({"id": "install-python", "argv": [".venv/bin/python", "-m", "pip", "install", "-e", "."]})
        if (root / "tests").is_dir():
            notes.append("Python tests detected; configure their actual runner (unittest, pytest, etc.).")
    if (root / "Cargo.toml").exists():
        checks += [{"id": "cargo-test", "argv": ["cargo", "test"]},
                   {"id": "cargo-check", "argv": ["cargo", "check"]}]
    if (root / "go.mod").exists():
        checks.append({"id": "go-test", "argv": ["go", "test", "./..."]})
    if not checks:
        notes.append("No acceptance command inferred. Add at least one [[checks]] before run/check/submit.")
    if (root / "AGENTS.md").exists():
        notes.append("Existing AGENTS.md will be read by Codex; it is not modified.")
    return setup, checks, notes


def initialize(root, provider=None, base=None):
    directory = root / ".devrelay"
    path = directory / "config.toml"
    if path.exists():
        raise RelayError(f"Configuration already exists: {path}; left unchanged")
    if directory.is_symlink() or (root / ".gitignore").is_symlink():
        raise RelayError("Configuration directory or .gitignore is a symlink; left unchanged")
    try:
        slug = github_slug(git(root, "remote", "get-url", "origin"))
    except RelayError:
        slug = ""
    provider = provider or ("github" if slug else "local")
    if provider == "github" and not slug:
        raise RelayError("Cannot infer a GitHub repository from origin; use --provider local or set origin")
    if base is None:
        try:
            base = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")
        except RelayError:
            base = next((name for name in ("main", "master") if has_ref(root, name)), None)
            if base is None:
                base = git(root, "branch", "--show-current") or "main"
    git(root, "check-ref-format", "--branch", base)
    setup, checks, notes = detect(root)
    q = json.dumps
    lines = ["# Review commands and acceptance before executing tasks.", "[project]",
             f"provider = {q(provider)}", f"base_branch = {q(base)}", 'remote = "origin"',
             'branch_prefix = "devrelay/task-"']
    if slug:
        lines.append(f"repository = {q(slug)}")
    lines += ["", "[queue]", 'approval_label = "auto:approved"',
              'priorities = ["priority:P0", "priority:P1", "priority:P2"]',
              'blockers = ["blocked", "needs-input"]', "", "[codex]",
              'executable = "codex"', 'sandbox = "workspace-write"',
              "timeout_seconds = 1800", "max_attempts = 2", "", "[delivery]",
              "auto_review = false", "auto_merge = false", "required_checks = []", "allow_no_ci = false",
              "cleanup_branches = false", "cleanup_worktrees = false"]
    for phase, entries in (("setup", setup), ("checks", checks)):
        for entry in entries:
            lines += ["", f"[[{phase}]]", f"id = {q(entry['id'])}", f"argv = {q(entry['argv'])}", "timeout_seconds = 600"]
    if not checks:
        lines += ["", "# Add your project acceptance, for example:", '# [[checks]]',
                  '# id = "test"', '# argv = ["python3", "-m", "unittest", "discover", "-s", "tests"]']
    directory.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    ignore = root / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    rules = ["/.devrelay/worktrees/", "/.devrelay/runtime/"]
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        rules += ["/.venv/", "__pycache__/", "*.py[cod]", "*.egg-info/"]
    additions = [line for line in rules if line not in text.splitlines()]
    if additions:
        with ignore.open("a") as stream:
            stream.write(("\n" if text and not text.endswith("\n") else "") + "\n".join(additions) + "\n")
    return {"config": str(path), "provider": provider, "checks": len(checks), "notes": notes}
