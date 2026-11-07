"""Scoped, recoverable cleanup only after exact merged-commit acceptance."""

import json
from pathlib import Path
import re
import uuid

from .errors import RelayError
from .git import (git, has_ref, is_ancestor, pinned_transport, remote_tracking_ref,
                  source_tree, validate_remote, worktrees)
from .runner import assert_config, base_commit
from .state import now


def _tree(config, path, head, branch=None):
    """Never remove an unregistered, moved, locked or modified checkout."""
    path = Path(path)
    records = [item for item in worktrees(config.root) if Path(item["worktree"]).resolve() == path.resolve()]
    if not records and not path.exists() and not path.is_symlink():
        return False
    if len(records) != 1 or not path.is_dir() or path.is_symlink():
        raise RelayError(f"Cleanup worktree is missing, unregistered or a symlink; preserved: {path}")
    record = records[0]
    if "locked" in record or record.get("HEAD") != head:
        raise RelayError(f"Cleanup worktree is locked or HEAD changed; preserved: {path}")
    if branch is not None and record.get("branch") != "refs/heads/" + branch:
        raise RelayError(f"Cleanup worktree uses a different branch; preserved: {path}")
    if branch is None and "detached" not in record:
        raise RelayError(f"Acceptance worktree is no longer detached; preserved: {path}")
    if (git(path, "status", "--porcelain", "--untracked-files=all")
            or source_tree(path) != git(path, "rev-parse", head + "^{tree}")):
        raise RelayError(f"Cleanup worktree has new changes; preserved: {path}")
    return True


def _remote_head(config, url, ref, *, env=None):
    output = git(config.root, "ls-remote", url, ref, env=env)
    if not output:
        return None
    fields = output.split()
    if len(fields) != 2 or fields[1] != ref or not re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
        raise RelayError("Remote branch identity is unclear; cleanup stopped")
    return fields[0]


def run(config, state, task):
    """Keep acceptance and cleanup results separate; retry by inspecting actual Git state."""
    proof = task.get("post_merge") or {}
    if (not proof.get("passed") or proof.get("merge_sha") != task.get("merge_sha")
            or not config.matches_evidence(proof, "checks")
            or config.data["delivery"]["post_merge_checks"] and not proof.get("commands_run", True)
            or proof.get("source_tree") != (task.get("verification") or {}).get("source_tree")):
        raise RelayError("Cleanup requires successful current-config post-merge acceptance")
    policy = config.data["delivery"]
    receipt = {"status": "running", "started_at": now(), "merge_sha": task["merge_sha"],
               "config_sha256": config.fingerprint, "cleanup_branches": policy["cleanup_branches"],
               "cleanup_worktrees": policy["cleanup_worktrees"], "steps": []}
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-cleanup")
    directory.mkdir(parents=True)
    task.update(status="cleaning", cleanup=receipt, cleanup_path=str(directory / "cleanup.json"), error=None)

    def save():
        Path(task["cleanup_path"]).write_text(json.dumps(receipt, indent=2) + "\n")
        state.save(task)

    def step(name, action):
        receipt["steps"].append({"id": name, "status": "running"})
        save()  # Checkpoint before the mutation; timeouts are resolved from actual refs on retry.
        result = action()
        receipt["steps"][-1].update(status=result, finished_at=now())
        save()

    save()
    try:
        assert_config(config)
        push_url = validate_remote(config)
        base_commit(config)
        if (not is_ancestor(config.root, task["merge_sha"], config.base_ref)
                or git(config.root, "rev-parse", task["merge_sha"] + "^{tree}") != proof["source_tree"]):
            raise RelayError("Validated merge is absent or different; cleanup stopped")
        expected = config.root / ".devrelay/worktrees" / task["id"]
        task_path = Path(task["worktree"])
        branch = task["branch"]
        if task_path != expected or branch != config.prefix + task["id"] or branch == config.base:
            raise RelayError("Cleanup target differs from this task's owned worktree/branch")
        ref = "refs/heads/" + branch
        post_path = task.get("post_merge_worktree")
        if post_path:
            path = Path(post_path)
            if (path.parent != expected.parent
                    or not re.fullmatch(re.escape(task["id"]) + r"-post-[0-9a-f]{32}", path.name)):
                raise RelayError("Acceptance worktree is outside its expected task path")
            _tree(config, path, task["merge_sha"])
        if policy["cleanup_worktrees"]:
            _tree(config, task_path, task["head_sha"], branch)
        if policy["cleanup_branches"]:
            if has_ref(config.root, ref) and git(config.root, "rev-parse", ref) != task["head_sha"]:
                raise RelayError("Local task branch advanced; preserved")
            others = [item for item in worktrees(config.root) if item.get("branch") == ref
                      and Path(item["worktree"]).resolve() != task_path.resolve()]
            if others:
                raise RelayError("Task branch is checked out in another worktree; preserved")
            with pinned_transport(config.root, push_url) as (target, env):
                remote_head = _remote_head(config, target, ref, env=env)
            if remote_head is not None and remote_head != task["head_sha"]:
                raise RelayError("Remote task branch advanced; preserved")

        def remove(path, head, branch=None):
            assert_config(config)
            if not _tree(config, path, head, branch):
                return "already_absent"
            git(config.root, "worktree", "remove", str(path))  # No force or prune of unrelated registrations.
            return "removed"

        # Successful acceptance scratch trees are disposable even when task cleanup is disabled.
        if post_path:
            step("acceptance_worktree", lambda: remove(Path(post_path), task["merge_sha"]))
            task.pop("post_merge_worktree", None)
            save()
        if policy["cleanup_worktrees"]:
            step("task_worktree", lambda: remove(task_path, task["head_sha"], branch))
        if policy["cleanup_branches"]:
            def remote():
                assert_config(config)
                current_push_url = validate_remote(config)
                with pinned_transport(config.root, current_push_url) as (target, env):
                    current = _remote_head(config, target, ref, env=env)
                    if current is None:
                        return "already_absent"
                    if current != task["head_sha"]:
                        raise RelayError("Remote task branch advanced; preserved")
                    failure = None
                    try:
                        git(config.root, "push", "--force-with-lease=" + ref + ":" + task["head_sha"],
                            target, ":" + ref, env=env)
                    except RelayError as error:
                        failure = error
                    if _remote_head(config, target, ref, env=env) is not None:
                        raise failure or RelayError("Remote task branch still exists after deletion")
                return "removed"
            step("remote_branch", remote)

            def local():
                assert_config(config)
                if not has_ref(config.root, ref):
                    return "already_absent"
                if any(item.get("branch") == ref for item in worktrees(config.root)):
                    raise RelayError("Task branch was checked out again; preserved")
                # Squash does not preserve commit ancestry. Delete only the exact accepted head with CAS.
                git(config.root, "update-ref", "--no-deref", "-d", ref, task["head_sha"])
                return "removed"
            step("local_branch", local)

            def tracking():
                assert_config(config)
                tracking_ref = remote_tracking_ref(config.root, config.remote, branch)
                if tracking_ref is None or not has_ref(config.root, tracking_ref):
                    return "already_absent"
                git(config.root, "update-ref", "--no-deref", "-d", tracking_ref, task["head_sha"])
                return "removed"
            step("remote_tracking_branch", tracking)
        receipt.update(status="completed", finished_at=now())
        task.update(status="completed", error=None)
        task.pop("handoff", None)
        save()
        return task
    except (RelayError, OSError, KeyboardInterrupt) as error:
        receipt.update(status="failed", error=str(error) or "Cleanup interrupted", finished_at=now())
        if receipt["steps"] and receipt["steps"][-1]["status"] == "running":
            receipt["steps"][-1].update(status="failed", error=receipt["error"])
        task.update(status="cleanup_failed", error=receipt["error"])
        save()
        raise
