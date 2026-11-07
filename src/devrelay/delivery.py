"""Opt-in PR review, exact-head merge gates and fresh post-merge acceptance."""

import json
from pathlib import Path
import re
import uuid

from . import codex
from .discovery import attach_sources, capture
from .cleanup import run as cleanup
from .errors import RelayError
from .git import command, git, is_ancestor, source_tree, validate_remote
from .process import execute
from .providers import authorized, gh, task_id
from .runner import assert_config, base_commit, claimed_task, require_prepared
from .state import now
from .triage import scope_digest


PULL_QUERY = """query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  id mergeQueueEntry{id} autoMergeRequest{enabledAt}
  url state headRefName headRefOid baseRefName baseRefOid isCrossRepository
  isDraft mergeable mergeStateStatus reviewDecision mergeCommit{oid}
  reviewThreads(first:100){nodes{isResolved} pageInfo{hasNextPage}}
  latestReviews(first:100){nodes{state} pageInfo{hasNextPage}}
  commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){
   nodes{__typename ... on CheckRun{name status conclusion detailsUrl}
    ... on StatusContext{context state targetUrl}} pageInfo{hasNextPage}
  }}}}}
 }}
}"""


def pull_snapshot(config, task):
    """Pin host/repository and fail closed on truncated or unavailable API data."""
    url = task.get("pr_url", "")
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9]\d*)", url)
    if not match or match[1].casefold() != config.repo.casefold():
        raise RelayError("Saved PR URL does not match the configured GitHub repository")
    owner, name = config.repo.split("/")
    raw = command(["gh", "api", "--hostname", "github.com", "graphql", "-f", "query=" + PULL_QUERY,
                   "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + match[2]], config.root)
    try:
        response = json.loads(raw)
        if response.get("errors"):
            raise ValueError("GraphQL errors")
        pull = response["data"]["repository"]["pullRequest"]
        if not isinstance(pull, dict) or pull["url"].casefold() != url.casefold():
            raise ValueError("PR identity")
        for key in ("state", "headRefName", "headRefOid", "baseRefName", "baseRefOid", "mergeable", "mergeStateStatus"):
            if not isinstance(pull[key], str):
                raise ValueError(key)
        for key in ("isCrossRepository", "isDraft"):
            if type(pull[key]) is not bool:
                raise ValueError(key)
        for key in ("reviewThreads", "latestReviews"):
            _connection(pull[key])
        commits = pull["commits"]["nodes"]
        if len(commits) != 1 or commits[0]["commit"]["oid"] != pull["headRefOid"]:
            raise ValueError("head checks")
        rollup = commits[0]["commit"]["statusCheckRollup"]
        pull["checks"] = _connection(rollup["contexts"]) if rollup else []
        if not isinstance(pull["id"], str) or not pull["id"]:
            raise ValueError("PR node ID")
        for key, field in (("mergeQueueEntry", "id"), ("autoMergeRequest", "enabledAt")):
            value = pull[key]
            if value is not None and (not isinstance(value, dict) or not isinstance(value[field], str) or not value[field]):
                raise ValueError(key)
        pull["reviewDecision"]
        pull["mergeCommit"]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise RelayError("GitHub returned incomplete or invalid PR gate data") from error
    return pull


def _connection(value):
    if value["pageInfo"]["hasNextPage"] is not False or not isinstance(value["nodes"], list):
        raise ValueError("Truncated connection; at most 100 checks/reviews/threads are supported")
    if any(not isinstance(item, dict) for item in value["nodes"]):
        raise ValueError("Invalid connection node")
    return value["nodes"]


def identity(task, config):
    record = task["verification"]
    return {"head_sha": task["head_sha"], "base_sha": record["base_sha"],
            "source_tree": record["source_tree"], "config_sha256": config.stage_fingerprint("review"),
            "task_body": record["task_body"], "task_title": record["task_title"], "pr_url": task["pr_url"],
            **({"scope_sha256": record["scope_sha256"]} if record.get("scope_sha256") else {})}


def current_review(task, config):
    record = task.get("review") or {}
    return bool(record and task.get("verification")) and all(record.get(key) == value for key, value in identity(task, config).items())


def request_identity(task, config):
    # Delivery policy changes still invalidate a queued mutation, without rerunning setup/checks.
    return {**identity(task, config), "delivery_sha256": config.stage_fingerprint("delivery")}


def open_evidence(config, state, task, pull):
    assert_config(config)
    require_prepared(config, task)
    fresh = authorized(config, state, task["id"])
    record = task.get("verification") or {}
    path = Path(task["worktree"])
    if (not record.get("passed") or not config.matches_evidence(record, "checks")
            or record.get("task_body") != fresh["body"] or record.get("task_title") != fresh["title"]
            or record.get("scope_sha256") != scope_digest(config, fresh)
            or not task.get("head_sha") or git(path, "rev-parse", "HEAD") != task["head_sha"]
            or git(path, "rev-parse", "HEAD^{tree}") != record.get("source_tree")
            or source_tree(path) != record.get("source_tree")):
        raise RelayError("PR verification is missing or stale; check and submit the task again")
    if (pull["state"] != "OPEN" or pull["headRefName"] != task["branch"]
            or pull["headRefOid"] != task["head_sha"] or pull["baseRefName"] != config.base
            or pull["isCrossRepository"]):
        raise RelayError("PR identity or HEAD differs from the verified submission")
    base = base_commit(config)
    if pull["baseRefOid"] != base or record["base_sha"] != base or not is_ancestor(path, base, task["head_sha"]):
        raise RelayError("Target base changed; integrate the base, check, submit and review again")


def review(config, state, identifier, *, recover=False):
    if config.provider != "github":
        raise RelayError("PR review requires project.provider=github")
    task = claimed_task(config, state, identifier)
    try:
        pull = pull_snapshot(config, task)
        open_evidence(config, state, task, pull)
        if recover:
            if task.get("review_identity") != identity(task, config):
                raise RelayError("Saved review identity is missing or stale; explicitly review again")
            directory = codex.review_directory(state, task, Path(task.get("review_run", "")))
            try:
                receipt_path = codex.review_file(state, task, directory, "events.jsonl.receipt.json")
                receipt = json.loads(receipt_path.read_text())
                if (not isinstance(receipt, dict) or type(receipt.get("returncode")) is not int
                        or receipt["returncode"] != 0 or receipt.get("timed_out") is not False
                        or receipt.get("cwd") != task["worktree"]
                        or receipt.get("log") != str(directory / "events.jsonl")
                        or receipt.get("argv") != codex.build_command(config, directory, sandbox="read-only")):
                    raise ValueError("Review process receipt does not match this successful review run")
                result = codex.read_review(state, task, directory)
            except (OSError, ValueError) as error:
                raise RelayError("Interrupted review has no complete evidence; explicitly review again") from error
        else:
            task.update(status="reviewing", review=None, error=None, review_identity=identity(task, config))
            directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-review")
            task["review_run"] = str(directory)
            state.save(task)
            result = codex.review(config, state, task, directory)
        open_evidence(config, state, task, pull_snapshot(config, task))
        record = {**result, **identity(task, config), "finished_at": now()}
        receipt = codex.review_file(state, task, directory, "review.json")
        receipt.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        task.update(review=record, review_path=str(receipt),
                    status="reviewed" if result["verdict"] == "approve" else "review_blocked")
        if result["verdict"] != "approve":
            task["error"] = "; ".join(result["findings"]) or result["summary"]
        else:
            task.pop("handoff", None)
        state.save(task)
        task = capture(config, state, task, result, origin="review", report_path=directory / "result.json")
        return state.save(task)
    except (RelayError, OSError, KeyboardInterrupt) as error:
        task.update(status="review_blocked", review=None, error=str(error) or "Review interrupted")
        state.save(task)
        raise


def gate_reasons(config, pull):
    reasons, seen = [], set()
    required = config.data["delivery"]["required_checks"]
    if pull["isDraft"]:
        reasons.append("PR is still draft")
    if pull["mergeable"] != "MERGEABLE" or pull["mergeStateStatus"] != "CLEAN":
        reasons.append(f"GitHub merge state: {pull['mergeable']}/{pull['mergeStateStatus']}")
    if pull["reviewDecision"] not in (None, "APPROVED"):
        reasons.append(f"GitHub review decision: {pull['reviewDecision']}")
    for item in pull["reviewThreads"]["nodes"]:
        if item.get("isResolved") is not True:
            reasons.append("Unresolved review thread")
    for item in pull["latestReviews"]["nodes"]:
        if item.get("state") not in ("APPROVED", "COMMENTED", "DISMISSED"):
            reasons.append("Outstanding or unknown GitHub review state")
    for item in pull["checks"]:
        if item.get("__typename") == "CheckRun":
            name = item.get("name")
            passed = item.get("status") == "COMPLETED" and (item.get("conclusion") == "SUCCESS"
                     or item.get("conclusion") == "SKIPPED" and name not in required)
        elif item.get("__typename") == "StatusContext":
            name = item.get("context")
            passed = item.get("state") == "SUCCESS"
        else:
            name, passed = None, False
        if not isinstance(name, str) or not name:
            reasons.append("Unknown CI check")
        else:
            seen.add(name)
            if not passed:
                reasons.append(f"CI check is not successful: {name}")
    for name in config.data["delivery"]["required_checks"]:
        if name not in seen:
            reasons.append(f"Required CI check is missing: {name}")
    if not pull["checks"] and not config.data["delivery"]["allow_no_ci"]:
        reasons.append("No CI evidence; configure CI or explicitly set delivery.allow_no_ci=true")
    return list(dict.fromkeys(reasons))


def blocking_gates(pull, config=None):
    """Permanent failures stop watch; pending CI/approvals/protection may be polled."""
    blockers = []
    required = config.data["delivery"]["required_checks"] if config else []
    if pull["mergeable"] == "CONFLICTING" or pull["mergeStateStatus"] == "DIRTY":
        blockers.append("Merge conflict")
    if pull["reviewDecision"] == "CHANGES_REQUESTED":
        blockers.append("Changes requested")
    if any(item.get("isResolved") is not True for item in pull["reviewThreads"]["nodes"]):
        blockers.append("Unresolved review thread")
    if any(item.get("state") not in ("APPROVED", "COMMENTED", "DISMISSED", "PENDING")
           for item in pull["latestReviews"]["nodes"]):
        blockers.append("Outstanding or unknown GitHub review state")
    for item in pull["checks"]:
        kind = item.get("__typename")
        if (not isinstance(item.get("name" if kind == "CheckRun" else "context"), str)
                or not item.get("name" if kind == "CheckRun" else "context")
                or kind == "CheckRun" and item.get("status") not in ("COMPLETED", "QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED")
                or kind == "CheckRun" and item.get("status") == "COMPLETED"
                and not (item.get("conclusion") == "SUCCESS" or item.get("conclusion") == "SKIPPED"
                         and item.get("name") not in required)
                or kind == "StatusContext" and item.get("state") not in ("SUCCESS", "PENDING")
                or kind not in ("CheckRun", "StatusContext")):
            blockers.append("Failed or unknown CI check")
    return blockers


def abort_pending(config, state, task, reason):
    """Best-effort revocation of our saved request, with durable remote readback."""
    request = task.get("merge_request")
    if not request or request.get("state") == "cancelled":
        return task
    cancellation = {"reason": reason, "at": now(), "status": "started"}
    task["merge_cancellation"] = cancellation
    state.save(task)
    try:
        if request.get("pr_url") != task.get("pr_url"):
            raise RelayError("Saved request PR identity differs; cancellation requires manual inspection")
        validate_remote(config)
        pull = pull_snapshot(config, task)
        if pull["state"] == "MERGED":
            task["merge_sha"] = (pull.get("mergeCommit") or {}).get("oid")
            cancellation["status"] = "already_merged"
        else:
            # Use GraphQL: gh --disable-auto refuses a PR already in the merge queue.
            for field, mutation, argument, output in (
                    ("autoMergeRequest", "disablePullRequestAutoMerge", "pullRequestId", "pullRequest{id}"),
                    ("mergeQueueEntry", "dequeuePullRequest", "id", "mergeQueueEntry{id}")):
                if pull.get(field):
                    query = f"mutation($id:ID!){{{mutation}(input:{{{argument}:$id}}){{{output}}}}}"
                    raw = command(["gh", "api", "--hostname", "github.com", "graphql", "-f", "query=" + query,
                                   "-f", "id=" + pull["id"]], config.root)
                    response = json.loads(raw)
                    if response.get("errors") or not response.get("data", {}).get(mutation):
                        raise RelayError("GitHub did not confirm pending merge cancellation")
            pull = pull_snapshot(config, task)
            if pull["state"] == "MERGED":
                task["merge_sha"] = (pull.get("mergeCommit") or {}).get("oid")
                cancellation["status"] = "already_merged"
            elif pull.get("mergeQueueEntry") or pull.get("autoMergeRequest"):
                raise RelayError("Remote merge remains pending; remove it on GitHub before recovery")
            else:
                cancellation["status"] = "cancelled"
                request["state"] = "cancelled"
        task.update(status="merge_blocked", error=reason)
    except (RelayError, OSError, ValueError, TypeError, AttributeError) as error:
        cancellation.update(status="failed", error=str(error))
        task.update(status="merge_blocked", error=f"{reason}; pending merge cancellation failed: {error}")
    return state.save(task)


def post_merge(config, state, task, pull):
    """Recover a successful remote merge without issuing another merge request."""
    oid = (pull.get("mergeCommit") or {}).get("oid", "")
    if (pull["state"] != "MERGED" or pull["baseRefName"] != config.base
            or pull["headRefOid"] != task.get("head_sha") or pull["headRefName"] != task["branch"]
            or pull["isCrossRepository"] or not re.fullmatch(r"[0-9a-f]{40,64}", oid)):
        raise RelayError("Merged PR does not match the saved submission")
    # This durable checkpoint must precede all local verification: failures cannot undo a merge.
    previous = task.get("post_merge_worktree")
    if previous and not (task.get("post_merge") or {}).get("passed"):
        retained = task.setdefault("retained_worktrees", [])
        if previous not in retained:
            retained.append(previous)
        task.pop("post_merge_worktree", None)  # A policy change must not clean a failed acceptance checkout.
    task.update(status="merged", merge_sha=oid, error=None)
    state.save(task)
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-post-merge")
    directory.mkdir(parents=True)
    record = {"passed": False, "merge_sha": oid, "config_sha256": config.fingerprint,
              "checks_sha256": config.stage_fingerprint("checks"), "checks": [], "setup": [],
              "commands_run": config.data["delivery"]["post_merge_checks"]}
    path = config.root / ".devrelay/worktrees" / (task["id"] + "-post-" + uuid.uuid4().hex)
    try:
        assert_config(config)
        base_commit(config)
        if not is_ancestor(config.root, oid, config.base_ref):
            raise RelayError("Merge commit is absent from the refreshed target branch")
        tree = git(config.root, "rev-parse", oid + "^{tree}")
        record["source_tree"] = tree
        if tree != task["verification"]["source_tree"]:
            raise RelayError("Merged tree differs from the reviewed source; inspect the base/merge race before continuing")
        if record["commands_run"] and not config.data["checks"]:
            raise RelayError("No post-merge acceptance commands configured")
        if record["commands_run"]:
            git(config.root, "worktree", "add", "--detach", str(path), oid)
            task["post_merge_worktree"] = str(path)
            state.save(task)
        for phase in (("setup", "checks") if record["commands_run"] else ()):
            if phase == "checks" and (source_tree(path) != tree or git(path, "rev-parse", "HEAD") != oid):
                raise RelayError("Source or HEAD changed during post-merge setup")
            for entry in config.data[phase]:
                receipt = execute(state, entry["argv"], path, directory / (phase + "-" + entry["id"] + ".log"), entry["timeout_seconds"])
                record[phase].append({"id": entry["id"], **receipt})
                if phase == "checks" and (source_tree(path) != tree or git(path, "rev-parse", "HEAD") != oid):
                    raise RelayError("Source or HEAD changed during post-merge acceptance")
        assert_config(config)
        if record["commands_run"] and (source_tree(path) != tree or git(path, "rev-parse", "HEAD") != oid):
            raise RelayError("Source or HEAD changed during post-merge acceptance")
        base_commit(config)
        if not is_ancestor(config.root, oid, config.base_ref):
            raise RelayError("Merge commit disappeared from the target branch during acceptance")
        record.update(passed=True, finished_at=now())
    except (RelayError, KeyboardInterrupt) as error:
        record.update(error=str(error) or "Post-merge acceptance interrupted", finished_at=now())
        task.update(status="post_merge_failed", error=record["error"])
        raise
    finally:
        receipt_path = directory / "verification.json"
        receipt_path.write_text(json.dumps(record, indent=2) + "\n")
        task.update(post_merge=record, post_merge_path=str(receipt_path))
        state.save(task)
    return cleanup(config, state, task)


def merge(config, state, identifier, *, retry_request=False):
    try:
        return _merge(config, state, identifier, retry_request=retry_request)
    except (RelayError, KeyboardInterrupt) as error:
        task = state.get(str(identifier))
        if task and (config.data["delivery"]["auto_merge"] or task.get("merge_sha") or task.get("merge_request")):
            if isinstance(error, KeyboardInterrupt) and task.get("merge_request") and task["status"] in ("merging", "merge_waiting"):
                task["status"] = "merging"
            elif task["status"] == "completed":
                task["status"] = "post_merge_failed"
            elif task["status"] not in ("merged", "post_merge_failed", "review_blocked", "cleanup_failed"):
                task["status"] = "merge_blocked"
            task["error"] = str(error) or "Merge interrupted; read back GitHub before retrying"
            state.save(task)
            if isinstance(error, RelayError) and task.get("merge_request") and not task.get("merge_sha"):
                abort_pending(config, state, task, task["error"])
        raise


def _merge(config, state, identifier, *, retry_request=False):
    if config.provider != "github":
        raise RelayError("Automatic PR merge requires project.provider=github")
    task = state.get(task_id(identifier))
    if not task or not task.get("pr_url"):
        raise RelayError(f"Task {identifier} has no submitted PR")
    validate_remote(config)
    pull = pull_snapshot(config, task)
    if pull["state"] == "MERGED":
        if ((task.get("post_merge") or {}).get("passed")
                and config.matches_evidence(task["post_merge"], "checks")
                and (not config.data["delivery"]["post_merge_checks"] or task["post_merge"].get("commands_run", True))):
            if (pull["headRefOid"] != task["head_sha"] or pull["headRefName"] != task["branch"]
                    or pull["baseRefName"] != config.base or pull["isCrossRepository"]
                    or (pull["mergeCommit"] or {}).get("oid") != task["merge_sha"]):
                raise RelayError("Merged PR readback differs from the completed task")
            assert_config(config)
            base_commit(config)
            if not is_ancestor(config.root, task["merge_sha"], config.base_ref):
                raise RelayError("Previously verified merge is no longer in the target branch")
            return cleanup(config, state, task)
        return post_merge(config, state, task, pull)
    if not config.data["delivery"]["auto_merge"]:
        raise RelayError("Merge is disabled; opt in with [delivery] auto_merge=true in project configuration")
    open_evidence(config, state, task, pull)
    request = task.get("merge_request")
    pending = bool(pull.get("mergeQueueEntry") or pull.get("autoMergeRequest"))
    if request:
        if not current_review(task, config) or task["review"]["verdict"] != "approve":
            raise RelayError("Pending merge has no current approved independent review")
        if pending and not all(request.get(key) == value for key, value in request_identity(task, config).items()):
            raise RelayError("Saved merge request evidence differs; inspect it before retrying")
        if pending:
            blockers = blocking_gates(pull, config)
            if blockers:
                raise RelayError("; ".join(blockers))
            task.update(status="merge_waiting", error="Merge request is pending on GitHub")
            task["merge_gates"] = {"head_sha": task["head_sha"], "base_sha": pull["baseRefOid"],
                                   "checked_at": now(), "reasons": gate_reasons(config, pull),
                                   "blockers": [], "checks": pull["checks"]}
            return state.save(task)
        if not retry_request:
            raise RelayError("Saved merge request is no longer queued or its outcome is uncertain; "
                             "inspect GitHub, then use devrelay merge ID --retry-request to retry explicitly")
        task.setdefault("merge_request_history", []).append(request)
        task.pop("merge_request")
        state.save(task)
    elif pending:
        raise RelayError("PR has a remote pending merge without a saved request; inspect GitHub before continuing")
    if not current_review(task, config):
        task = review(config, state, identifier, recover=task["status"] == "reviewing")
    if task["review"]["verdict"] != "approve":
        task.update(status="review_blocked", error="; ".join(task["review"]["findings"]) or task["review"]["summary"])
        return state.save(task)
    pull = pull_snapshot(config, task)
    open_evidence(config, state, task, pull)
    if pull["isDraft"]:
        gh(config, "pr", "ready", task["pr_url"])
    pull = pull_snapshot(config, task)
    open_evidence(config, state, task, pull)
    reasons = gate_reasons(config, pull)
    task["merge_gates"] = {"head_sha": task["head_sha"], "base_sha": pull["baseRefOid"],
                           "checked_at": now(), "reasons": reasons, "blockers": blocking_gates(pull, config), "checks": pull["checks"]}
    if reasons:
        task.update(status="merge_waiting", error="; ".join(reasons))
        return state.save(task)
    task.update(status="merging", error=None)
    state.save(task)
    assert_config(config)
    # Re-read task authorization and base immediately before the exact-head mutation.
    open_evidence(config, state, task, pull)
    pull = pull_snapshot(config, task)
    open_evidence(config, state, task, pull)
    reasons = gate_reasons(config, pull)
    if reasons:
        task.update(status="merge_waiting", error="; ".join(reasons))
        task["merge_gates"].update(reasons=reasons, blockers=blocking_gates(pull, config), checked_at=now(), checks=pull["checks"])
        return state.save(task)
    if pull.get("mergeQueueEntry") or pull.get("autoMergeRequest"):
        raise RelayError("PR acquired a remote pending merge; inspect GitHub before continuing")
    task["merge_request"] = {**request_identity(task, config), "state": "started", "requested_at": now()}
    state.save(task)  # Crash/timeout after this point must never silently resend the mutation.
    request_error = None
    try:
        gh(config, "pr", "merge", task["pr_url"], "--squash", "--match-head-commit", task["head_sha"])
    except RelayError as error:
        request_error = error
    task["merge_request"].update(state="uncertain" if request_error else "accepted",
                                 error=str(request_error) if request_error else None)
    state.save(task)
    pull = pull_snapshot(config, task)
    if pull["state"] == "MERGED":
        return post_merge(config, state, task, pull)
    if request_error:
        raise request_error
    if not (pull.get("mergeQueueEntry") or pull.get("autoMergeRequest")):
        raise RelayError("Merge request returned without a merged or queued PR; inspect GitHub before retrying")
    task.update(status="merge_waiting", error="Merge is pending on GitHub; rerun devrelay merge to read back the result")
    return state.save(task)


def automatic_review(config, task):
    return config.provider == "github" and (config.data["delivery"]["auto_review"]
            or config.data["delivery"]["auto_merge"]
            or bool(task.get("discovery_source") and config.data["discovery"]["auto_review"]))


def advance(config, state, identifier):
    """After an explicit submit, run only the delivery phases enabled by this project."""
    settings = config.data["delivery"]
    if settings["auto_merge"]:
        return merge(config, state, identifier)
    saved = state.get(str(identifier))
    if saved and automatic_review(config, attach_sources(config, state, [saved])[0]):
        task = claimed_task(config, state, identifier)
        if current_review(task, config):
            try:
                open_evidence(config, state, task, pull_snapshot(config, task))
            except RelayError as error:
                task.update(status="review_blocked", review=None, error=str(error))
                state.save(task)
                raise
            verdict = task["review"]["verdict"]
            task.update(status="reviewed" if verdict == "approve" else "review_blocked",
                        error=None if verdict == "approve" else "; ".join(task["review"]["findings"]) or task["review"]["summary"])
            return state.save(task)
        return review(config, state, identifier, recover=task["status"] == "reviewing")
    return state.get(str(identifier))
