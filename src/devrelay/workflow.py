"""Task-centred continuation shared by managed execution and agent-facing guidance."""

import json
from pathlib import Path

from .errors import RelayError
from .git import git, is_ancestor, source_tree
from .providers import authorized
from .runner import (assert_config, base_commit, claimed_task, editable_task,
                     implement_task, prepare, prepared, start, submit, verify)
from .state import now
from .triage import author_reason, automatic, automatic_enabled, authorization_reason, current as current_triage, scope_digest, triage


def guidance(config, task):
    """Read-only local advice; live authorization and evidence are checked on execution."""
    from .delivery import automatic_review
    identifier, status = task["id"], task["status"]
    reason = author_reason(config, task)
    if reason and status != "completed":
        return {"kind": "authorize", "actor": "human", "message": reason,
                "next": "Check the Issue author and queue.allowed_authors in .devrelay/config.toml"}
    if automatic(config, task) and config.data["queue"]["approval_label"] not in task["labels"]:
        if (task.get("triage") or {}).get("phase") in ("preparing", "failed"):
            return {"kind": "attention", "actor": "human", "message": task["triage"].get("error", "Investigation interrupted"),
                    "next": f"Inspect the retained triage context, then triage {identifier} --retry"}
        if current_triage(config, task) and task["triage"]["result"]["decision"] != "ready":
            result = task["triage"]["result"]
            return {"kind": result["decision"], "actor": "human" if result["decision"] == "needs_input" or task.get("worktree") else "none",
                    "message": result["summary"], "questions": result["questions"],
                    "next": f"Update Issue {identifier}'s body with the decision, or explicitly authorize it; triage {identifier} --retry re-investigates unchanged input"}
        if authorization_reason(config, task):
            return {"kind": "triage", "actor": "agent", "message": "Investigate current Issue and project scope before execution",
                    "next": f"triage {identifier}", "tools": f"triage {identifier} --prepare; triage {identifier} --file RESULT.json"}
    handoff = task.get("handoff")
    if handoff:
        return handoff
    if status == "queued" and authorization_reason(config, task):
        return {"kind": "authorize", "actor": "human", "message": "Review the discovered task before authorizing execution",
                "next": f"devrelay task approve {identifier}" if config.provider == "local" else
                        f"Apply {config.data['queue']['approval_label']} on GitHub"}
    if status == "failed" and (task.get("failure") or {}).get("kind") == "checks":
        return {"kind": "repair", "actor": "agent", "feedback": task["failure"],
                "next": f"Read the check logs, fix the worktree, then check {identifier} and submit {identifier}"}
    review = task.get("review") or {}
    if status == "review_blocked" and review.get("verdict") == "changes_requested":
        return {"kind": "repair", "actor": "agent", "message": review["summary"],
                "feedback": review["findings"], "next": f"Fix the worktree, then check {identifier} and submit {identifier}"}
    if status in ("failed", "blocked", "start_failed", "review_blocked", "merge_blocked",
                  "post_merge_failed", "cleanup_failed"):
        return {"kind": "attention", "actor": "human", "message": task.get("error"),
                "next": f"Inspect saved evidence, resolve the cause, then work {identifier} --retry"}
    if status == "merge_waiting":
        return {"kind": "waiting", "actor": "environment", "message": task.get("error"),
                "next": f"work {identifier}"}
    if status == "completed" or status == "committed" and config.provider == "local":
        return {"kind": "delivered", "actor": "none", "next": "Integrate the local commit" if status == "committed" else None}
    if task.get("pr_url") and (status == "reviewed" or status == "pr_open" and not automatic_review(config, task)) and not config.data["delivery"]["auto_merge"]:
        return {"kind": "review", "actor": "human", "next": "Review and merge the delivered PR"}
    return {"kind": "continue", "actor": "agent", "next": f"work {identifier}",
            "tools": f"prepare {identifier}, implement in worktree, check {identifier}, submit {identifier}"}


def pause(state, task, kind, message, *, actor="human", feedback=None):
    task["handoff"] = {"kind": kind, "actor": actor, "message": message,
                       "requirements": {key: task[key] for key in ("title", "body")},
                       "labels": list(task["labels"]),
                       "feedback": feedback, "next": f"Resolve the cause, then work {task['id']} --retry"}
    return state.save(task)


def parkable(config, task):
    """Only known task-local handoffs can release the serial worker."""
    handoff = task.get("handoff") or {}
    blockers = set((task.get("merge_gates") or {}).get("blockers", []))
    return (automatic(config, task) and not task.get("merge_request") and not task.get("merge_sha")
            and (handoff.get("kind") == "budget" or blockers <= {"Changes requested", "Unresolved review thread"})
            and handoff.get("kind") in ("input", "budget")
            and task["status"] not in ("merging", "merged", "post_merge_failed", "cleaning", "cleanup_failed"))


def integrate_base(config, state, task):
    """Advance a clean task branch without resetting, stashing or force-pushing work."""
    base = base_commit(config)
    path = Path(task["worktree"])
    if is_ancestor(path, base, "HEAD"):
        return False
    editable_task(config, task)
    if source_tree(path) != git(path, "rev-parse", "HEAD^{tree}"):
        raise RelayError("Target base advanced while the worktree has edits; preserve them and integrate the base before retrying")
    task.setdefault("base_updates", []).append({"from": git(path, "rev-parse", "HEAD"), "to": base, "at": now()})
    task.update(verification=None, review=None, status="integrating")
    state.save(task)
    # A failed merge retains conflicts and the original index for inspection. Never abort/reset it.
    git(path, "merge", "--no-edit", "--no-overwrite-ignore", base)
    task.update(status="prepared", base_sha=base)
    state.save(task)
    return True


def repair_feedback(config, state, task):
    """Only actionable machine findings enter the automatic repair loop."""
    from .delivery import current_review
    review = task.get("review") or {}
    if task["status"] == "review_blocked" and review.get("verdict") == "changes_requested" and current_review(task, config):
        return {"kind": "review", "summary": review["summary"], "findings": review["findings"],
                "evidence": task.get("review_path")}
    gates = task.get("merge_gates") or {}
    if task["status"] == "merge_waiting" and set(gates.get("blockers", [])) == {"Failed or unknown CI check"}:
        failed = [check for check in gates.get("checks", [])
                  if (check.get("__typename") == "CheckRun" and check.get("status") == "COMPLETED"
                      and check.get("conclusion") in ("FAILURE", "TIMED_OUT"))
                  or check.get("__typename") == "StatusContext" and check.get("state") == "FAILURE"]
        from .delivery import blocking_gates
        remaining = {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": None,
                     "reviewThreads": {"nodes": []}, "latestReviews": {"nodes": []},
                     "checks": [check for check in gates.get("checks", []) if check not in failed]}
        if failed and not blocking_gates(remaining, config):
            return {"kind": "ci", "checks": failed,
                    "instruction": "Investigate the failed checks and reproduce the failure before fixing it. "
                                   "Do not weaken acceptance or CI policy. Report blocked if logs or infrastructure are unavailable."}
    return None


def work(config, state, identifier=None, *, retry=False):
    """Continue one task through implementation, checks, repair and configured delivery.

    The implementation-turn budget survives process restarts and CI waits. Only an
    explicit retry opens another budget; low-level tools never invoke this loop.
    """
    from .delivery import advance, abort_pending, pull_snapshot
    if not config.data["checks"]:
        raise RelayError("Configure at least one [[checks]] before work")
    if identifier is None:
        from .worker import selection
        selected = selection(config, state)
        if selected["status"] != "selected":
            return selected
        identifier = selected["id"]
    identifier = str(identifier)
    task = state.get(identifier)
    if automatic_enabled(config) and not (task or {}).get("merge_request") and not (task or {}).get("merge_sha"):
        from .providers import snapshot
        fresh = next((t for t in snapshot(config, state).tasks if t["id"] == identifier), None)
        if fresh and automatic(config, fresh) and authorization_reason(config, fresh):
            task = triage(config, state, identifier, retry=retry)
            if task["triage"]["result"]["decision"] != "ready":
                if task.get("worktree"):
                    result = task["triage"]["result"]
                    return pause(state, task, "input", result["summary"], feedback=result["questions"])
                return task
    if not task or not task.get("worktree"):
        task = start(config, state, identifier)
    cycle = task.get("workflow")
    if cycle is None or retry:
        if cycle:
            task.setdefault("workflow_history", []).append(cycle)
        cycle = {"used": 0, "limit": config.data["codex"]["max_attempts"], "started_at": now()}
        task.update(workflow=cycle)
        task.pop("handoff", None)
        if retry and task.get("pr_url") and task["status"] in ("reviewing", "review_blocked") and (task.get("review") or {}).get("verdict") != "changes_requested":
            task.update(status="pr_open", review=None)
        state.save(task)
    elif task.get("handoff"):
        return task
    feedback = task.get("repair_feedback")
    try:
        for _ in range(2 * cycle["limit"] + 4):  # Implementation/delivery turns, plus recovery; respect configured budgets.
            assert_config(config)
            task = state.get(identifier)
            if task.get("merge_sha") or task.get("merge_request") or task["status"] in ("completed", "cleaning", "cleanup_failed", "post_merge_failed"):
                if config.provider == "local":
                    return task
                from .delivery import merge
                return merge(config, state, identifier)
            fresh = authorized(config, state, identifier)
            task = claimed_task(config, state, identifier)
            if task.get("pr_url"):
                pull = pull_snapshot(config, task)
                if pull["state"] == "MERGED":
                    from .delivery import merge
                    return merge(config, state, identifier)
                editable_task(config, task)
                if pull.get("autoMergeRequest") or pull.get("mergeQueueEntry"):
                    raise RelayError("PR has a remote pending merge; inspect it before changing source")
            record = task.get("verification")
            needs_check = bool(record and not config.matches_evidence(record, "checks"))
            if task.get("setup_fingerprint") and not prepared(config, task):
                task = prepare(config, state, task)
                needs_check = True
            if record and (record["task_title"] != fresh["title"] or record["task_body"] != fresh["body"]
                           or record.get("scope_sha256") != scope_digest(config, fresh)):
                feedback = {"kind": "requirements", "message": "Task requirements changed; implement and verify the current authorized scope"}
            if integrate_base(config, state, task) or needs_check:
                task = state.get(identifier)
                if not prepared(config, task):
                    task = prepare(config, state, task)
                try:
                    task = verify(config, state, identifier)
                except RelayError:
                    task = state.get(identifier)
                    if (task.get("failure") or {}).get("kind") != "checks":
                        raise
                    feedback = task["failure"]
            if not feedback and task["status"] in ("verified", "committed"):
                task = submit(config, state, identifier, local=config.provider == "local")
                if config.provider == "local":
                    return task
            elif not feedback and task.get("pr_url") and task["status"] not in ("prepared", "failed", "implementing", "interrupted"):
                task = advance(config, state, identifier)
            else:
                if cycle["used"] >= cycle["limit"]:
                    return pause(state, task, "budget", "Implementation/repair budget exhausted; inspect evidence before another attempt",
                                 feedback=feedback)
                cycle["used"] += 1
                task.update(workflow=cycle, repair_feedback=feedback)
                task.pop("handoff", None)
                state.save(task)  # Charge before starting a process, including interrupted turns.
                try:
                    task = implement_task(config, state, identifier, resume=True, allow_pr=True,
                                          max_attempts=1, feedback=json.dumps(feedback, ensure_ascii=False) if feedback else "")
                except RelayError:
                    task = state.get(identifier)
                    if (task.get("failure") or {}).get("kind") != "checks":
                        raise
                    feedback = task["failure"]
                    task["repair_feedback"] = feedback
                    state.save(task)
                    if cycle["used"] >= cycle["limit"]:
                        pause(state, task, "budget", "Acceptance failed and the implementation budget is exhausted", feedback=feedback)
                        raise
                    continue
                if task["status"] == "blocked":
                    return pause(state, task, "input", task.get("error"))
                feedback = None
                task.pop("repair_feedback", None)
                state.save(task)
                continue
            feedback = repair_feedback(config, state, task)
            if feedback:
                # Feedback is durable before the next turn so a crash cannot bypass it.
                task["repair_feedback"] = feedback
                state.save(task)
                if cycle["used"] >= cycle["limit"]:
                    return pause(state, task, "budget", "Review/CI repair budget exhausted", feedback=feedback)
                continue
            if automatic(config, task) and task["status"] == "review_blocked" and (task.get("review") or {}).get("verdict") == "blocked":
                return pause(state, task, "input", task.get("error"), feedback=task["review"]["findings"])
            if task["status"] in ("review_blocked", "merge_blocked", "post_merge_failed", "cleanup_failed"):
                return pause(state, task, "attention", task.get("error"))
            if task["status"] == "merge_waiting" and task.get("merge_gates", {}).get("blockers"):
                task["status"] = "merge_blocked"
                return pause(state, task, "input", task.get("error"))
            return task
        return pause(state, task, "environment", "Target state kept changing; inspect it before retrying")
    except (RelayError, OSError, KeyboardInterrupt) as error:
        task = state.get(identifier) or task
        if isinstance(error, KeyboardInterrupt):
            raise
        if task.get("merge_request") and not task.get("merge_sha"):
            task = abort_pending(config, state, task, str(error))
        elif task.get("pr_url") and config.data["delivery"]["auto_merge"] and task["status"] not in ("review_blocked", "post_merge_failed", "cleanup_failed"):
            task.update(status="merge_blocked", error=str(error))
        if not task.get("handoff"):
            pause(state, task, "attention", str(error))
        raise
