"""Agent-callable engineering operations and optional single-task orchestration."""

import json
from pathlib import Path
import re
import uuid

from . import codex
from .discovery import capture
from .config import load
from .errors import RelayError
from .git import (command, git, has_ref, is_ancestor, pinned_transport, remote_tracking_ref,
                  source_tree, stage_submodule_gitlinks, validate_remote, worktrees)
from .process import execute
from .providers import authorized, gh, gh_json, queue_report, snapshot, task_id
from .state import now
from .triage import autonomous, automatic, automatic_enabled, authorization_reason, scope_digest, triage


def assert_config(config):
    if load(config.root).fingerprint != config.fingerprint:
        raise RelayError("Project configuration changed during this operation; rerun with the new configuration")


def require_prepared(config, task):
    if config.data["setup"] and not prepared(config, task):
        raise RelayError(f"Project setup is missing or stale; run devrelay prepare {task['id']} before acceptance or delivery")


def prepared(config, task):
    return config.matches_evidence({"config_sha256": task.get("setup_fingerprint"),
                                    **({"setup_sha256": task["setup_sha256"]} if "setup_sha256" in task else {})}, "setup")


def publication_heads(task):
    allowed = {task.get("published_head", task.get("head_sha"))}
    if (intent := task.get("push_intent")) and intent["previous_sha"] in allowed:
        allowed.add(intent["head_sha"])
    return allowed - {None}


def editable_task(config, task):
    if task.get("merge_sha") or task["status"] in ("completed", "merging", "merged", "post_merge_failed", "cleaning", "cleanup_failed"):
        raise RelayError("Task is merging or already integrated; recover delivery instead of preparing its environment")
    if task.get("pr_url"):
        from .delivery import pull_snapshot
        validate_remote(config)
        pull = pull_snapshot(config, task)
        if (pull["state"] != "OPEN" or pull["headRefName"] != task["branch"]
                or pull["headRefOid"] not in publication_heads(task) or pull["baseRefName"] != config.base
                or pull["isCrossRepository"] or pull.get("autoMergeRequest") or pull.get("mergeQueueEntry")):
            raise RelayError("PR is closed, merged or differs from the saved submission, or has a pending merge; recover PR identity/state before preparation")


def submission_evidence(config, state, task, record, *, head=None):
    """Re-read every mutable input before each publication boundary."""
    assert_config(config)
    require_prepared(config, task)
    fresh = authorized(config, state, task["id"])
    path = Path(task["worktree"])
    if (not record or not record.get("passed") or record["source_tree"] != source_tree(path)
            or not config.matches_evidence(record, "checks") or record["base_sha"] != base_commit(config)
            or record["task_body"] != fresh["body"] or record["task_title"] != fresh["title"]
            or record.get("scope_sha256") != scope_digest(config, fresh)):
        raise RelayError("Verification is missing or stale; task requirements/source/configuration/base may have changed; run devrelay check before submitting")
    if head is not None and git(path, "rev-parse", "HEAD") != head:
        raise RelayError("HEAD changed before publication; rerun verification")
    return fresh


def base_commit(config):
    if config.provider == "github":
        validate_remote(config)
        git(config.root, "fetch", config.remote, config.base)
    if not has_ref(config.root, config.base_ref):
        raise RelayError(f"Base {config.base_ref} has no commit; create an initial commit or configure the correct base")
    return git(config.root, "rev-parse", config.base_ref)


def claimed_task(config, state, identifier):
    from .discovery import attach_sources
    task = state.get(task_id(identifier))
    if not task or not task.get("worktree"):
        raise RelayError(f"Task {identifier} has no worktree; run devrelay start {identifier}")
    path = Path(task["worktree"])
    expected = config.root / ".devrelay/worktrees" / task["id"]
    if path.resolve() != expected.resolve() or not path.is_dir():
        raise RelayError("Saved worktree is missing or outside its expected path; preserve state and repair the checkout")
    if git(path, "branch", "--show-current") != task["branch"]:
        raise RelayError("Existing worktree uses a different branch; left unchanged")
    records = worktrees(config.root)
    if not any(Path(item["worktree"]).resolve() == path.resolve()
               and item.get("branch") == "refs/heads/" + task["branch"] for item in records):
        raise RelayError("Worktree is not registered with this repository")
    return attach_sources(config, state, [task])[0]


def start(config, state, identifier):
    identifier = task_id(identifier)
    task = authorized(config, state, identifier)
    saved = state.get(identifier)
    if saved and saved["status"] == "completed":
        raise RelayError(f"Task {identifier} is already completed; inspect its saved delivery evidence")
    if saved and saved.get("worktree") and Path(saved["worktree"]).is_dir():
        return claimed_task(config, state, identifier)
    recovering_start = saved and saved["status"] in ("claimed", "start_failed")
    view = snapshot(config, state, ignore_state_claim=identifier if recovering_start else None)
    report = queue_report(config, view)
    if identifier not in {item["id"] for item in report["eligible"]}:
        raise RelayError(f"Task {identifier} already has work or a PR; recover it instead")
    base = base_commit(config)
    path = config.root / ".devrelay/worktrees" / identifier
    branch = config.prefix + identifier
    if path.exists():
        raise RelayError(f"Unregistered worktree directory exists: {path}; left unchanged")
    git(config.root, "check-ref-format", "--branch", branch)
    task = {**(saved or {}), **task, "status": "claimed", "branch": branch, "worktree": str(path),
            "base_sha": base, "attempts": (saved or {}).get("attempts", 0)}
    state.save(task)  # Persist ownership before creating the worktree.
    try:
        if has_ref(config.root, "refs/heads/" + branch):
            git(config.root, "worktree", "add", str(path), branch)
        else:
            git(config.root, "worktree", "add", "-b", branch, str(path), base)
    except RelayError:
        task["status"] = "start_failed"
        state.save(task)
        raise
    return state.save(task)


def prepare(config, state, task):
    assert_config(config)
    editable_task(config, task)
    task["status"] = "preparing"
    task["setup_fingerprint"] = None
    task.pop("setup_sha256", None)
    task["verification"] = None
    task["review"] = None
    task["merge_gates"] = None
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-setup")
    task["setup_path"] = str(directory)
    state.save(task)
    try:
        directory.mkdir(parents=True)
        for entry in config.data["setup"]:
            assert_config(config)
            authorized(config, state, task["id"])
            execute(state, entry["argv"], task["worktree"], directory / (entry["id"] + ".log"), entry["timeout_seconds"])
        assert_config(config)
        authorized(config, state, task["id"])
        editable_task(config, task)
    except (RelayError, OSError, KeyboardInterrupt) as error:
        task.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                    error=str(error) or "Interrupted")
        state.save(task)
        raise
    task["setup_fingerprint"] = config.fingerprint
    task["setup_sha256"] = config.stage_fingerprint("setup")
    task["status"] = "prepared"
    task["error"] = None
    return state.save(task)


def prepare_task(config, state, identifier):
    """Explicit setup for an external agent; never start a Codex session."""
    task = claimed_task(config, state, identifier)
    fresh = authorized(config, state, identifier)
    task.update({key: fresh[key] for key in ("body", "title", "labels", "author") if key in fresh})
    return prepare(config, state, task)


def verify(config, state, identifier):
    if not config.data["checks"]:
        raise RelayError("Configure at least one [[checks]] acceptance command before verification")
    task = claimed_task(config, state, identifier)
    require_prepared(config, task)
    fresh = authorized(config, state, identifier)
    task.update({key: fresh[key] for key in ("body", "title", "labels", "author") if key in fresh})
    path = Path(task["worktree"])
    base = base_commit(config)
    if not is_ancestor(path, base, "HEAD"):
        raise RelayError("Target base moved beyond this worktree; integrate it into the task branch and rerun verification")
    before = source_tree(path)
    head = git(path, "rev-parse", "HEAD")
    directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-checks")
    directory.mkdir(parents=True)
    task["status"] = "verifying"
    task["verification"] = None
    task["verification_path"] = str(directory / "verification.json")
    task.pop("failure", None)
    state.save(task)
    results = []
    try:
        for entry in config.data["checks"]:
            receipt = execute(state, entry["argv"], path, directory / (entry["id"] + ".log"), entry["timeout_seconds"])
            results.append({"id": entry["id"], **receipt})
            if source_tree(path) != before or git(path, "rev-parse", "HEAD") != head:
                raise RelayError("Source or HEAD changed during verification; rerun the checks")
        assert_config(config)
        if source_tree(path) != before or git(path, "rev-parse", "HEAD") != head:
            raise RelayError("Source or HEAD changed during verification; rerun the checks")
        if base_commit(config) != base:
            raise RelayError("Target base changed during verification; integrate it and rerun")
        latest = authorized(config, state, identifier)
        if (latest["body"] != fresh["body"] or latest["title"] != fresh["title"]
                or scope_digest(config, latest) != scope_digest(config, fresh)):
            raise RelayError("Task requirements changed during verification; resume and revalidate")
        if source_tree(path) != before or git(path, "rev-parse", "HEAD") != head:
            raise RelayError("Source or HEAD changed while finalizing verification; rerun the checks")
    except (RelayError, OSError, KeyboardInterrupt) as error:
        message = str(error) or "Interrupted"
        task.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=message)
        if not isinstance(error, KeyboardInterrupt):
            task["failure"] = {"kind": "checks", "message": message, "logs": str(directory)}
        state.save(task)
        (directory / "verification.json").write_text(json.dumps({"passed": False, "error": message, "checks": results}, indent=2))
        raise
    record = {"passed": True, "source_tree": before, "head_sha": head, "base_sha": base,
              "config_sha256": config.fingerprint, "task_body": fresh["body"], "task_title": fresh["title"],
              "checks_sha256": config.stage_fingerprint("checks"), "scope_sha256": scope_digest(config, fresh),
              "checks": results, "finished_at": now()}
    receipt_path = directory / "verification.json"
    receipt_path.write_text(json.dumps(record, indent=2) + "\n")
    task.update(status="verified", verification=record, verification_path=str(receipt_path), base_sha=base, error=None)
    for key in ("handoff", "failure", "repair_feedback"):
        task.pop(key, None)
    return state.save(task)


def run_task(config, state, identifier=None, *, resume=False, submit_after=False):
    if submit_after:
        from .workflow import work
        return work(config, state, identifier, retry=resume)
    return implement_task(config, state, identifier, resume=resume)


def implement_task(config, state, identifier=None, *, resume=False, allow_pr=False, feedback=None, max_attempts=None):
    if not config.data["checks"]:
        raise RelayError("Configure at least one [[checks]] before running an AI task")
    if autonomous(config) and not allow_pr and identifier is None:
        from .worker import selection
        selected = selection(config, state)
        if selected["status"] != "selected":
            return selected
        identifier = selected["id"]
        resume = bool((state.get(identifier) or {}).get("worktree"))
    if identifier is None:
        delivery_attention = [item["id"] for item in state.tasks()
                              if item["status"] in ("review_blocked", "merge_blocked", "post_merge_failed", "cleanup_failed")]
        if delivery_attention:
            return {"status": "needs_attention", "tasks": delivery_attention,
                    "message": "Inspect review/merge/post-merge evidence before dispatching new work"}
        interrupted = [item for item in state.tasks() if item["status"] in ("claimed", "prepared", "preparing", "implementing", "verifying", "interrupted")]
        if interrupted:
            identifier = sorted(interrupted, key=lambda item: item["created_at"])[0]["id"]
            resume = True
        else:
            attention = [item["id"] for item in state.tasks() if item["status"] in ("failed", "blocked", "start_failed")]
            if attention:
                return {"status": "needs_attention", "tasks": attention,
                        "message": "Inspect saved errors and explicitly resume these tasks before new automatic dispatch"}
            report = queue_report(config, snapshot(config, state))
            selected = report["selected"] or next(iter(report.get("triage_candidates", [])), None)
            if selected is None:
                return {"status": "idle", "message": "No eligible task"}
            identifier = selected["id"]
    if automatic_enabled(config) and not allow_pr:
        fresh = next((task for task in snapshot(config, state).tasks if task["id"] == str(identifier)), None)
        if fresh and automatic(config, fresh) and authorization_reason(config, fresh):
            investigated = triage(config, state, identifier)
            if investigated["triage"]["result"]["decision"] != "ready":
                return investigated
    if resume and (state.get(str(identifier)) or {}).get("status") in ("claimed", "start_failed"):
        task = start(config, state, identifier)
    else:
        task = claimed_task(config, state, identifier) if resume else start(config, state, identifier)
    if task.get("pr_url") and not allow_pr:
        raise RelayError(f"Task {identifier} already has a PR; use check/submit, review or merge")
    if task["status"] == "completed":
        raise RelayError(f"Task {identifier} is already {task['status']}; inspect status instead")
    fresh = authorized(config, state, identifier)
    task.update({key: fresh[key] for key in ("body", "title", "labels", "author") if key in fresh})
    if allow_pr:
        editable_task(config, task)
    feedback = feedback if feedback is not None else task.get("error", "")
    limit = max_attempts or config.data["codex"]["max_attempts"]
    try:
        if not prepared(config, task):
            task = prepare(config, state, task)
        for attempt in range(limit):
            fresh = authorized(config, state, identifier)
            task.update({key: fresh[key] for key in ("body", "title", "labels", "author") if key in fresh})
            task.update(status="implementing", attempts=task.get("attempts", 0) + 1, verification=None)
            task.pop("failure", None)
            state.save(task)
            directory = state.directory / "runs" / task["id"] / (uuid.uuid4().hex + "-codex")
            before_head = git(task["worktree"], "rev-parse", "HEAD")
            result = codex.implement(config, state, task, directory, feedback)
            task["summary"] = result["summary"]
            if git(task["worktree"], "rev-parse", "HEAD") != before_head:
                raise RelayError("Codex changed Git history; preserve its work and review before continuing")
            assert_config(config)
            task = capture(config, state, task, result, origin="implementation", report_path=directory / "result.json")
            if result["outcome"] == "blocked":
                task.update(status="blocked", error="; ".join(result["blockers"]) or result["summary"])
                return state.save(task)
            state.save(task)
            try:
                task = verify(config, state, task["id"])
            except RelayError as error:
                feedback = str(error)
                if attempt + 1 >= limit:
                    raise
                task = claimed_task(config, state, identifier)
                fresh = authorized(config, state, identifier)
                task.update({key: fresh[key] for key in ("body", "title", "labels", "author") if key in fresh})
                continue
            return task
    except (RelayError, KeyboardInterrupt) as error:
        task = state.get(str(identifier)) or task
        if not task.get("pr_url"):
            task["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        task["error"] = str(error) or "Interrupted"
        state.save(task)
        raise


SUBMISSION_QUERY = """query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
  url mergeQueueEntry{id} autoMergeRequest{enabledAt}
 }}
}"""


def submission_merge_state(config, state, task, number):
    owner, name = config.repo.split("/")
    raw = command(["gh", "api", "--hostname", "github.com", "graphql", "-f", "query=" + SUBMISSION_QUERY,
                   "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=" + number], config.root)
    try:
        response = json.loads(raw)
        task["pr_observation"]["merge"] = response
        state.save(task)
        if response.get("errors"):
            raise ValueError("GraphQL errors")
        pull = response["data"]["repository"]["pullRequest"]
        if not isinstance(pull, dict) or pull["url"].casefold() != task["pr_url"].casefold():
            raise ValueError("PR identity")
        for key, field in (("mergeQueueEntry", "id"), ("autoMergeRequest", "enabledAt")):
            value = pull[key]
            if value is not None and (not isinstance(value, dict) or not isinstance(value[field], str) or not value[field]):
                raise ValueError(key)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise RelayError("GitHub returned incomplete or invalid PR merge state; preserve delivery and inspect GitHub") from error
    return pull


def submission_pull(config, state, task, *, heads=None):
    if not task.get("pr_url"):
        pulls = gh_json(config, "pr", "list", "--head", task["branch"],
                        "--state", "all", "--limit", "100", "--json", "number,url")
        if len(pulls) > 1:
            raise RelayError("Multiple PRs own this task branch; inspect delivery before retrying")
        if not pulls:
            return None
        task["pr_url"] = pulls[0]["url"]
        state.save(task)
    try:
        url = task["pr_url"]
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9]\d*)", url)
        if not match or match[1].casefold() != config.repo.casefold():
            raise RelayError("Saved PR URL differs from the configured repository; preserve delivery and inspect GitHub")
        pull = gh_json(config, "pr", "view", url, "--json",
                       "url,state,headRefName,headRefOid,baseRefName,isCrossRepository,autoMergeRequest")
        task["pr_observation"] = {"checked_at": now(), "pull": pull}
        state.save(task)
        if not isinstance(pull, dict) or any(not isinstance(pull.get(key), str) for key in
                ("url", "state", "headRefName", "headRefOid", "baseRefName")):
            raise RelayError("GitHub returned incomplete PR identity; preserve delivery and inspect GitHub")
        merge_state = submission_merge_state(config, state, task, match[2])
        allowed = heads if heads is not None else publication_heads(task)
        if (pull.get("url", "").casefold() != url.casefold() or pull.get("state") != "OPEN"
                or pull.get("headRefName") != task["branch"] or pull.get("headRefOid") not in allowed
                or pull.get("baseRefName") != config.base or pull.get("isCrossRepository") is not False
                or "autoMergeRequest" not in pull or pull["autoMergeRequest"] is not None
                or merge_state["mergeQueueEntry"] is not None or merge_state["autoMergeRequest"] is not None
                or task.get("merge_request")):
            raise RelayError("PR readback differs from the saved submission or is closed/merged/pending; preserve delivery and inspect GitHub")
    except RelayError as error:
        task.update(status="blocked", error=str(error))
        state.save(task)
        raise
    task["published_head"] = pull["headRefOid"]
    if intent := task.get("push_intent"):
        if pull["headRefOid"] == intent["head_sha"]:
            task.pop("push_intent")
    state.save(task)
    return pull


def submit(config, state, identifier, *, local=False):
    if config.provider != "github" and not local:
        raise RelayError("Local tasks use submit --local to create a verified local commit")
    saved = state.get(str(identifier))
    if saved and saved.get("pr_url") and saved["status"] in ("completed", "cleaning", "cleanup_failed") and not local:
        from .delivery import advance
        return advance(config, state, identifier)
    task = claimed_task(config, state, identifier)
    if task.get("pr_url") and task["status"] not in ("verified", "committed") and not local:
        from .delivery import advance, automatic_review
        if not automatic_review(config, task):
            validate_remote(config)
            submission_pull(config, state, task)
            submission_evidence(config, state, task, task.get("verification"), head=task.get("head_sha"))
        return advance(config, state, identifier)
    if not local:
        validate_remote(config)
        submission_pull(config, state, task)
    record = task.get("verification")
    path = Path(task["worktree"])
    fresh = submission_evidence(config, state, task, record)
    # Support retry after a commit/push/API timeout without creating a duplicate commit.
    current = git(path, "rev-parse", "HEAD")
    if current != record["head_sha"] and current != task.get("head_sha"):
        raise RelayError("HEAD changed since verification; rerun checks")
    git(path, "add", "-A")
    stage_submodule_gitlinks(path)
    if git(path, "write-tree") != record["source_tree"]:
        raise RelayError("Staged source differs from verified source")
    if git(path, "diff", "--cached", "--name-only", "--ignore-submodules=none"):
        git(path, "commit", "-m", f"devrelay: {fresh['title']} ({identifier})")
    head = git(path, "rev-parse", "HEAD")
    if source_tree(path) != record["source_tree"] or git(path, "rev-parse", "HEAD^{tree}") != record["source_tree"]:
        raise RelayError("Commit hooks changed verified content; rerun checks")
    if not int(git(path, "rev-list", "--count", f"{config.base_ref}..HEAD")):
        raise RelayError("No implementation commit to submit")
    submission_evidence(config, state, task, record, head=head)
    task.update(status="committed", head_sha=head)
    state.save(task)
    if local:
        return task
    submission_pull(config, state, task)
    submission_evidence(config, state, task, record, head=head)
    push_url = validate_remote(config, path)
    ref = "refs/heads/" + task["branch"]
    # Pin the exact validated endpoint in the transport invocation. A remote
    # name could fan out, while a later rewrite could redirect a plain URL.
    tracking_ref = remote_tracking_ref(path, config.remote, task["branch"])
    task["push_intent"] = {"head_sha": head, "previous_sha": task.get("published_head"), "started_at": now()}
    state.save(task)
    with pinned_transport(path, push_url) as (target, env):
        git(path, "push", target, ref + ":" + ref, env=env)
    if tracking_ref is not None:
        git(path, "update-ref", tracking_ref, head)
    # Match `push --set-upstream` without requiring the task branch to be
    # covered by remote.<name>.fetch (single-branch clones commonly are not).
    git(path, "config", "branch." + task["branch"] + ".remote", config.remote)
    git(path, "config", "branch." + task["branch"] + ".merge", ref)
    submission_evidence(config, state, task, record, head=head)
    summary = task.get("summary") or "Implementation verified by configured project checks."
    check_lines = "\n".join(f"- `{item['id']}`: exit {item['returncode']}" for item in record["checks"])
    body = (f"{summary}\n\nVerification\n\n{check_lines}\n\n"
            f"Source tree: `{record['source_tree']}`\nBase: `{record['base_sha']}`\n"
            f"Configuration SHA-256: `{record['config_sha256']}`\n\nRefs #{identifier}\n")
    submission_pull(config, state, task, heads={head})
    submission_evidence(config, state, task, record, head=head)
    if task.get("pr_url"):
        gh(config, "pr", "edit", task["pr_url"], "--body-file", "-", input=body)
    else:
        url = gh(config, "pr", "create", "--draft", "--base", config.base, "--head", task["branch"],
                 "--title", fresh["title"], "--body-file", "-", input=body)
        task["pr_url"] = url
        state.save(task)
    pull = submission_pull(config, state, task, heads={head})
    task.update(status="pr_open", pr_url=pull["url"])
    state.save(task)
    submission_evidence(config, state, task, record, head=head)
    from .delivery import advance
    return advance(config, state, identifier)


def complete_local(config, state, identifier):
    if config.provider != "local":
        raise RelayError("GitHub task completion is recorded on GitHub after reviewed integration")
    task = claimed_task(config, state, identifier)
    assert_config(config)
    require_prepared(config, task)
    fresh = task if task["status"] == "completed" else authorized(config, state, identifier)
    record = task.get("verification") or {}
    path = Path(task["worktree"])
    head = task.get("head_sha")
    if (not head or not record.get("passed") or record.get("config_sha256") != config.fingerprint
            or record.get("task_body") != fresh["body"] or record.get("task_title") != fresh["title"]
            or record.get("scope_sha256") != scope_digest(config, fresh)
            or git(path, "rev-parse", "HEAD") != head
            or git(path, "rev-parse", head + "^{tree}") != record.get("source_tree")
            or source_tree(path) != record.get("source_tree")):
        raise RelayError("Latest verified content is not the delivered commit; check and submit before completing the task")
    if not is_ancestor(config.root, head, config.base_ref):
        raise RelayError("The submitted task commit must be integrated into the configured base before completion")
    task.update(status="completed", open=False)
    return state.save(task)
