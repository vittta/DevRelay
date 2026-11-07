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

    def stage_fingerprint(self, stage):
        """Bind receipts only to inputs that affect their stage, not scheduling policy."""
        inputs = {"project": self.data["project"], "setup": self.data["setup"]}
        if stage in ("checks", "review", "delivery"):
            inputs["checks"] = self.data["checks"]
        if stage in ("review", "delivery"):
            inputs["reviewer"] = {key: value for key, value in self.data["codex"].items()
                                  if key not in ("max_attempts", "sandbox")}
        if stage == "delivery":
            inputs.update(queue=self.data["queue"], delivery=self.data["delivery"])
            inputs["discovery_policy"] = {key: self.data["discovery"][key] for key in ("auto_triage", "auto_review")}
        if stage not in ("setup", "checks", "review", "delivery"):
            raise ValueError(f"Unknown evidence stage: {stage}")
        return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()

    def matches_evidence(self, record, stage):
        """Old receipts remain valid only under their original full configuration."""
        if not record:
            return False
        key = stage + "_sha256"
        return (record[key] == self.stage_fingerprint(stage) if key in record
                else record.get("config_sha256") == self.fingerprint)

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
    _keys(data, ("project", "queue", "codex", "delivery", "discovery", "setup", "checks"), "configuration")
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
    _keys(queue, ("approval_label", "allowed_authors", "priorities", "blockers", "mode", "auto_kinds", "scope", "max_dispatches"), "queue")
    queue.setdefault("allowed_authors", [])
    authors = queue["allowed_authors"]
    if (not isinstance(authors, list)
            or any(not isinstance(author, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?", author)
                   for author in authors)
            or len({author.casefold() for author in authors}) != len(authors)):
        raise RelayError("queue.allowed_authors must contain unique GitHub logins (case-insensitive)")
    queue.setdefault("mode", "manual")
    queue.setdefault("auto_kinds", ["bug"])
    queue.setdefault("scope", "Fix confirmed bugs with clear expected behavior and bounded acceptance.")
    queue.setdefault("max_dispatches", 20)
    if queue["mode"] not in ("manual", "autonomous"):
        raise RelayError("queue.mode must be manual or autonomous")
    kinds = queue["auto_kinds"]
    if (not isinstance(kinds, list) or not kinds or any(kind not in ("bug", "feature", "maintenance") for kind in kinds)
            or len(set(kinds)) != len(kinds)):
        raise RelayError("queue.auto_kinds must contain unique bug/feature/maintenance kinds")
    if not isinstance(queue["scope"], str) or not queue["scope"].strip() or len(queue["scope"]) > 8000:
        raise RelayError("queue.scope must be a nonempty string of at most 8000 characters")
    if type(queue["max_dispatches"]) is not int or not 1 <= queue["max_dispatches"] <= 1000:
        raise RelayError("queue.max_dispatches must be an integer between 1 and 1000")
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
    discovery = _table(data, "discovery")
    _keys(discovery, ("enabled", "max_per_task", "auto_triage", "auto_review"), "discovery")
    discovery.setdefault("enabled", True)
    for key in ("auto_triage", "auto_review"):
        discovery.setdefault(key, True)
        if type(discovery[key]) is not bool:
            raise RelayError(f"discovery.{key} must be a boolean")
    discovery.setdefault("max_per_task", 3)
    if type(discovery["enabled"]) is not bool:
        raise RelayError("discovery.enabled must be a boolean")
    if type(discovery["max_per_task"]) is not int or not 1 <= discovery["max_per_task"] <= 20:
        raise RelayError("discovery.max_per_task must be an integer between 1 and 20")
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
    _keys(delivery, ("auto_review", "auto_merge", "required_checks", "allow_no_ci", "cleanup_branches", "cleanup_worktrees", "post_merge_checks"), "delivery")
    delivery.setdefault("post_merge_checks", True)
    for key in ("auto_review", "auto_merge", "allow_no_ci", "cleanup_branches", "cleanup_worktrees", "post_merge_checks"):
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


def initial_configuration(root, provider=None, base=None, *, mode="manual"):
    """Discover and render a proposal without creating files or executing setup."""
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
    lines += ["", "[queue]", f'mode = {q(mode)}',
              '# GitHub author allowlist; empty allows any author. Set your and your automation account logins.',
              'allowed_authors = []', 'auto_kinds = ["bug"]',
              'scope = "Fix confirmed bugs with clear expected behavior and bounded acceptance."',
              'max_dispatches = 20', 'approval_label = "auto:approved"',
              'priorities = ["priority:P0", "priority:P1", "priority:P2"]',
              'blockers = ["blocked", "needs-input"]', "", "[codex]",
              'executable = "codex"', 'sandbox = "workspace-write"',
              "timeout_seconds = 1800", "max_attempts = 2", "", "[delivery]",
              "auto_review = false", "auto_merge = false", "required_checks = []", "allow_no_ci = false",
              "cleanup_branches = false", "cleanup_worktrees = false", "post_merge_checks = true",
              "", "[discovery]", "enabled = true",
              "auto_triage = true", "auto_review = true", "max_per_task = 3"]
    for phase, entries in (("setup", setup), ("checks", checks)):
        for entry in entries:
            lines += ["", f"[[{phase}]]", f"id = {q(entry['id'])}", f"argv = {q(entry['argv'])}", "timeout_seconds = 600"]
    if not checks:
        lines += ["", "# Add your project acceptance, for example:", '# [[checks]]',
                  '# id = "test"', '# argv = ["python3", "-m", "unittest", "discover", "-s", "tests"]']
    return "\n".join(lines) + "\n", {"provider": provider, "checks": len(checks), "notes": notes}


def ignore_additions(root):
    ignore = root / ".gitignore"
    if ignore.is_symlink() or ignore.exists() and not ignore.is_file():
        raise RelayError(".gitignore is a symlink or not a regular file; left unchanged")
    text = ignore.read_text() if ignore.exists() else ""
    rules = ["/.devrelay/worktrees/", "/.devrelay/runtime/"]
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        rules += ["/.venv/", "__pycache__/", "*.py[cod]", "*.egg-info/"]
    return text, [line for line in rules if line not in text.splitlines()]


def ensure_ignores(root):
    text, additions = ignore_additions(root)
    if additions:
        with (root / ".gitignore").open("a") as stream:
            stream.write(("\n" if text and not text.endswith("\n") else "") + "\n".join(additions) + "\n")


def initialize(root, provider=None, base=None, *, mode="manual", expected_source=None):
    directory = root / ".devrelay"
    path = directory / "config.toml"
    if path.exists() or path.is_symlink():
        raise RelayError(f"Configuration already exists: {path}; left unchanged")
    if directory.is_symlink():
        raise RelayError("Configuration directory is a symlink; left unchanged")
    ignore_additions(root)
    source, result = initial_configuration(root, provider, base, mode=mode)
    if expected_source is not None and source != expected_source:
        raise RelayError("Project inputs changed; check the project again before creating configuration")
    directory.mkdir(exist_ok=True)
    with path.open("x") as stream:
        stream.write(source)
    ensure_ignores(root)
    return {"config": str(path), **result}
