"""Git operations that preserve the caller's working tree and index."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .errors import RelayError


def command(argv, cwd, *, input=None, env=None, timeout=60, text=True):
    try:
        result = subprocess.run(argv, cwd=cwd, input=input, env=env, text=text,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"Cannot run {argv[0]}: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RelayError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result.stdout.strip() if text else result.stdout


def git(root, *argv, env=None, **kwargs):
    env = {**os.environ, **(env or {}), "GIT_NO_REPLACE_OBJECTS": "1"}
    return command(["git", *argv], root, env=env, **kwargs)


def _evidence_git(root, *argv, env=None, **kwargs):
    env = {**os.environ, **(env or {}), "GIT_NO_REPLACE_OBJECTS": "1"}
    return git(root, *argv, env=env, **kwargs)


def repository_root(path):
    return Path(git(Path(path).resolve(), "rev-parse", "--show-toplevel")).resolve()


def common_dir(root):
    path = Path(git(root, "rev-parse", "--git-common-dir"))
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def has_ref(root, ref):
    return subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                          cwd=root, capture_output=True,
                          env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}).returncode == 0


def is_ancestor(root, commit, ref):
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, ref],
                          cwd=root, capture_output=True,
                          env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}).returncode == 0


def _gitlink_paths(root, *, env=None):
    paths = []
    for entry in _evidence_git(root, "ls-files", "--stage", "-z", env=env, text=False).split(b"\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        fields = metadata.split()
        if separator and len(fields) == 3 and fields[0] == b"160000" and fields[2] == b"0":
            paths.append(os.fsdecode(path))
    return paths


def _submodule_checkout(root, path):
    candidate = root / path
    if candidate.is_symlink():
        return None
    checkout = candidate.resolve()
    if not checkout.is_dir() or not (checkout / ".git").exists():
        return None
    toplevel = _evidence_git(checkout, "rev-parse", "--show-toplevel", text=False).removesuffix(b"\n")
    return checkout if Path(os.fsdecode(toplevel)).resolve() == checkout else None


def _skipped_submodule_has_content(root, path):
    candidate = root / path
    if candidate.is_symlink():
        return True
    if not candidate.exists():
        return False
    if not candidate.is_dir():
        return True
    return next(candidate.iterdir(), None) is not None


def _submodule_heads(root, paths):
    heads = {}
    for path in paths:
        checkout = _submodule_checkout(root, path)
        if checkout is not None:
            heads[path] = _evidence_git(checkout, "rev-parse", "HEAD")
    return heads


def _content_matches_tree(root, tree):
    algorithm = _evidence_git(root, "rev-parse", "--show-object-format")
    if algorithm not in ("sha1", "sha256"):
        raise RelayError("Unsupported Git object format for source evidence")
    for entry in _evidence_git(root, "ls-tree", "-r", "-z", tree, text=False).split(b"\0"):
        if not entry:
            continue
        metadata, _, path = entry.partition(b"\t")
        mode, _, object_id = metadata.split()
        path = os.fsdecode(path)
        target = root / path
        if mode == b"120000":
            if not target.is_symlink():
                return False
            try:
                link_target = os.readlink(os.fsencode(target))
            except OSError as error:
                raise RelayError(f"Cannot read source symlink {path}: {error}") from error
            digest = hashlib.new(algorithm, usedforsecurity=False)
            digest.update(b"blob " + str(len(link_target)).encode() + b"\0" + link_target)
            if digest.hexdigest().encode() != object_id:
                return False
            continue
        if mode not in (b"100644", b"100755"):
            continue
        if target.is_symlink() or not target.is_file():
            return False
        # Hash raw blob bytes in-process. Do not trust the index stat cache, Git
        # filters or assume-unchanged flags. One Git process per file is prohibitive
        # for large pinned submodules used in joint evidence snapshots.
        try:
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    return False
                digest = hashlib.new(algorithm, usedforsecurity=False)
                digest.update(b"blob " + str(metadata.st_size).encode() + b"\0")
                count = 0
                while chunk := stream.read(1024 * 1024):
                    count += len(chunk)
                    digest.update(chunk)
                if count != metadata.st_size:
                    return False
        except OSError as error:
            raise RelayError(f"Cannot hash raw source {path}: {error}") from error
        if digest.hexdigest().encode() != object_id:
            return False
    return True


def _submodule_content_matches_head(root):
    return _content_matches_tree(root, "HEAD")


def _submodule_snapshot(root, paths, prefix=Path(), seen=None):
    heads, dirty = {}, []
    seen = set() if seen is None else seen
    for path in paths:
        display = (prefix / path).as_posix()
        checkout = _submodule_checkout(root, path)
        if checkout is None:
            if _skipped_submodule_has_content(root, path):
                raise RelayError(f"Submodule path is not initialized but contains files: {display}")
            continue
        if checkout in seen:
            continue
        seen.add(checkout)
        heads[display] = _evidence_git(checkout, "rev-parse", "HEAD")
        head_tree = _evidence_git(checkout, "rev-parse", "HEAD^{tree}")
        if (not _submodule_content_matches_head(checkout)
                or _evidence_git(checkout, "diff-index", "--cached", "--raw", "--ignore-submodules=none",
                                 "HEAD", "--", text=False)
                or _evidence_git(checkout, "-c", "core.ignoreCase=false", "ls-files", "--others",
                                 "--exclude-per-directory=.gitignore", "-z", text=False)
                or _isolated_source_tree(checkout, inspect_submodules=False,
                                         neutralize_config=True) != head_tree):
            dirty.append(display)
        nested_heads, nested_dirty = _submodule_snapshot(
            checkout, _gitlink_paths(checkout), prefix / path, seen)
        heads.update(nested_heads)
        dirty.extend(nested_dirty)
    return heads, dirty


def _clean_submodule_snapshot(root, paths):
    heads, dirty = _submodule_snapshot(root, paths)
    if dirty:
        raise RelayError("Submodule worktree has uncommitted changes: " + ", ".join(sorted(set(dirty))))
    return heads


def _isolated_source_tree(root, *, inspect_submodules, neutralize_config=False):
    with tempfile.TemporaryDirectory(prefix="devrelay-index-") as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index"), "GIT_OPTIONAL_LOCKS": "0"}
        _evidence_git(root, "read-tree", "HEAD", env=env, text=False)
        config = (["-c", "core.fileMode=true", "-c", "core.ignoreCase=false",
                   "-c", f"core.excludesFile={os.devnull}"]
                  if neutralize_config else [])
        _evidence_git(root, *config, "add", "-A", env=env, text=False)
        paths = _gitlink_paths(root, env=env)
        submodules = (_clean_submodule_snapshot(root, paths) if inspect_submodules
                      else _submodule_heads(root, paths))
        for path in paths:
            if path in submodules:
                _evidence_git(root, "update-index", "--add", "--cacheinfo", "160000", submodules[path], path,
                              env=env, text=False)
        if inspect_submodules:
            _evidence_git(root, "diff", "--cached", "--check", env=env, text=False)
        tree = _evidence_git(root, "write-tree", env=env)
        if inspect_submodules and not _content_matches_tree(root, tree):
            raise RelayError(
                "Working source differs from deliverable Git blobs; verification requires identical "
                "file bytes and symlink types. Use a checkout without byte-changing clean/smudge "
                "filters, line-ending normalization or working-tree encoding conversion. "
                "The worktree and original index were left unchanged"
            )
        if inspect_submodules and _clean_submodule_snapshot(root, paths) != submodules:
            raise RelayError("Submodule HEAD changed while creating source evidence; rerun verification")
        return tree


def source_tree(root):
    return _isolated_source_tree(Path(root), inspect_submodules=True)


def stage_submodule_gitlinks(root):
    paths = [path for path in _gitlink_paths(root) if _submodule_checkout(root, path) is not None]
    if paths:
        _evidence_git(root, "add", "--force", "--", *paths, text=False)


def github_slug(url):
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?",
        url,
    )
    return f"{match[1]}/{match[2]}" if match else ""


def _config_values(root, key):
    try:
        result = subprocess.run(["git", "config", "--get-all", key], cwd=root, text=True,
                                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"Cannot inspect Git configuration: {error}") from error
    if result.returncode == 1:
        return []
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RelayError(f"git failed ({result.returncode}): {detail}")
    return result.stdout.splitlines()


def _system_config_path(root):
    nosystem = os.environ.get("GIT_CONFIG_NOSYSTEM")
    if nosystem is not None:
        # Use Git's own boolean parser (including numeric and false spellings),
        # without loading configuration that this variable controls.
        try:
            disabled = git(root, "config", "--file", os.devnull, "--type=bool",
                           "--default", nosystem, "--get", "devrelay.nosystem")
        except RelayError as error:
            raise RelayError("Cannot parse GIT_CONFIG_NOSYSTEM as a Git boolean") from error
        if disabled == "true":
            return None
    configured = os.environ.get("GIT_CONFIG_SYSTEM")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else (Path(root) / path).resolve()
    try:
        result = subprocess.run(
            ["git", "config", "--system", "--includes", "--show-origin", "--null", "--list"],
            cwd=root, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"Cannot inspect Git system configuration: {error}") from error
    if result.returncode:
        if b"No such file or directory" in result.stderr:
            return None  # A missing system file is valid and contributes no settings.
        raise RelayError("Cannot inspect Git system configuration")
    entries = result.stdout.split(b"\0")
    for origin in entries[::2]:
        if origin.startswith(b"file:"):
            return Path(os.fsdecode(origin[5:]))
    return None


@contextmanager
def pinned_transport(root, url):
    """Yield an alias-proof transport argument and its pinned Git environment."""
    root = Path(root)
    system_config = _system_config_path(root)
    with tempfile.TemporaryDirectory(prefix="devrelay-transport-") as temporary:
        path = Path(temporary) / "gitconfig"
        # Git refuses remote configuration subsections beginning with '/'.
        # An absolute argument therefore cannot select a URL-named remote,
        # even if one is added after validation. Bind this non-existent path
        # to the approved endpoint in the same invocation as the transport.
        target = str((Path(temporary) / "endpoint").resolve())
        # System configuration is read before global, local, worktree and
        # command scopes. Git keeps the first equally long rewrite match, so
        # these exact self-mappings cannot be superseded by a later mutation.
        for name in ("insteadOf", "pushInsteadOf"):
            for source in (url, target):
                command(["git", "config", "--file", str(path), "--add",
                         f"url.{url}.{name}", source], root)
        if system_config is not None:
            command(["git", "config", "--file", str(path), "--add", "include.path",
                     str(system_config)], root)
        env = {**os.environ, "GIT_CONFIG_SYSTEM": str(path)}
        env.pop("GIT_CONFIG_NOSYSTEM", None)
        yield target, env


def remote_tracking_ref(root, remote, branch):
    """Map a pushed branch using the first matching positive fetch refspec."""
    ref = "refs/heads/" + branch
    specs = _config_values(root, f"remote.{remote}.fetch")

    def match(pattern):
        return re.fullmatch(re.escape(pattern).replace(r"\*", "(.*)"), ref)

    for spec in specs:
        # Negative refspecs filter fetch selection, not Git's positive
        # tracking mapping for a successfully pushed branch.
        if spec.startswith("^"):
            continue
        source, separator, destination = spec.removeprefix("+").partition(":")
        found = match(source)
        if separator and destination and found:
            mapped = destination.replace("*", found[1]) if "*" in source else destination
            git(root, "check-ref-format", mapped)
            return mapped
    return None


def validate_remote(config, root=None):
    if config.provider != "github":
        return None
    root = Path(root or config.root)
    raw_fetch = _config_values(root, f"remote.{config.remote}.url")
    raw_push = _config_values(root, f"remote.{config.remote}.pushurl") or raw_fetch
    raw_urls = {"fetch": raw_fetch, "push": raw_push}
    urls = {}
    for kind, argv in (("fetch", ("remote", "get-url", "--all", config.remote)),
                       ("push", ("remote", "get-url", "--push", "--all", config.remote))):
        # `remote get-url` expands url.*.insteadOf and pushInsteadOf, so these
        # are the endpoints Git would actually use rather than just raw config.
        urls[kind] = git(root, *argv).splitlines()
    if any(len(items) != 1 for group in (raw_urls, urls) for items in group.values()):
        raise RelayError(
            "Git remote must resolve to exactly one fetch URL and one push URL; "
            "multiple URLs and push fan-out are not allowed"
        )
    # Inspect the literal URL in this worktree too, rejecting chained and
    # conditional rewrites before constructing an alias-proof transport.
    literal_push_url = git(root, "ls-remote", "--get-url", urls["push"][0])
    if raw_urls != urls or literal_push_url != urls["push"][0]:
        raise RelayError(
            "Git URL rewrites are not allowed for the configured remote; "
            "use one explicit GitHub fetch URL and push URL"
        )
    for kind, items in urls.items():
        slug = github_slug(items[0])
        if slug.casefold() != config.repo.casefold():
            raise RelayError(
                f"Git {kind} URL does not match project.repository; "
                "correct the remote or URL rewrite before proceeding"
            )
    return urls["push"][0]


def worktrees(root):
    result = []
    for record in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        entry = {}
        for line in record.splitlines():
            key, _, value = line.partition(" ")
            entry[key] = value
        if entry:
            result.append(entry)
    return result
