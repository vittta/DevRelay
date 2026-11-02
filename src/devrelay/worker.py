"""Continuous serial dispatch with configurable manual or verified automatic merge."""

from time import sleep

from .delivery import advance, abort_pending, merge
from .errors import RelayError
from .process import interrupt_on_sigterm
from .providers import queue_report, snapshot
from .runner import assert_config, run_task, submit
from .state import now


IMPLEMENTING = {"claimed", "prepared", "preparing", "implementing", "verifying", "interrupted"}
SUBMITTING = {"verified", "committed"}
ATTENTION = {"failed", "blocked", "start_failed", "review_blocked", "merge_blocked",
             "post_merge_failed", "cleanup_failed"}
DELIVERING = {"pr_open", "reviewing", "reviewed", "merge_waiting", "merging", "merged", "cleaning"}


def selection(config, state):
    """Recover the oldest unfinished task before claiming new work."""
    tasks = state.tasks()
    attention = [task["id"] for task in tasks if task["status"] in ATTENTION]
    if attention:
        return {"status": "needs_attention", "tasks": attention,
                "message": "Inspect saved errors and explicitly recover these tasks before restarting watch"}
    pending = []
    for task in tasks:
        status = task["status"]
        if task.get("pr_url"):
            if (status in DELIVERING and config.data["delivery"]["auto_merge"]
                    or task.get("merge_request") and status in DELIVERING):
                pending.append((task, "merge"))
            elif status == "reviewing" or status == "pr_open" and config.data["delivery"]["auto_review"]:
                pending.append((task, "review"))
            elif status not in ("pr_open", "reviewed", "completed"):
                return {"status": "needs_attention", "tasks": [task["id"]],
                        "message": "Unexpected saved PR state; inspect it before restarting watch"}
        elif status in IMPLEMENTING:
            pending.append((task, "resume"))
        elif status in SUBMITTING:
            pending.append((task, "submit"))
        elif status not in ("queued", "completed"):
            return {"status": "needs_attention", "tasks": [task["id"]],
                    "message": "Unexpected saved task state; inspect it before restarting watch"}
    if pending:
        task, action = min(pending, key=lambda item: (item[0]["created_at"], item[0]["id"]))
        return {"status": "selected", "id": task["id"], "action": action}
    selected = queue_report(config, snapshot(config, state))["selected"]
    if selected is None:
        return {"status": "idle", "message": "No eligible task; waiting for authorized issues"}
    return {"status": "selected", "id": selected["id"], "action": "run"}


def watch(config, state, *, poll_interval=60, max_tasks=None, emit):
    """Own the repository lock for the entire foreground loop, including idle waits."""
    if config.provider != "github":
        raise RelayError("Continuous issue dispatch requires project.provider=github")
    if not config.data["checks"]:
        raise RelayError("Configure at least one [[checks]] before starting watch")
    if type(poll_interval) is not int or poll_interval < 1:
        raise RelayError("poll_interval must be a positive integer in seconds")
    if max_tasks is not None and (type(max_tasks) is not int or max_tasks < 1):
        raise RelayError("max_tasks must be a positive integer")

    def event(name, **fields):
        emit({"event": name, "at": now(), **fields})

    delivered = 0
    with interrupt_on_sigterm():
        with state.lock():
            automatic = config.data["delivery"]["auto_merge"]
            event("watch_started", poll_interval=poll_interval, max_tasks=max_tasks,
                  mode="auto_merge" if automatic else "manual_merge")
            idle = False
            try:
                while True:
                    assert_config(config)
                    selected = selection(config, state)
                    if selected["status"] == "needs_attention":
                        return {**selected, "delivered": delivered}
                    if selected["status"] == "idle":
                        if not idle:
                            event("idle", message=selected["message"])
                        idle = True
                        sleep(poll_interval)
                        continue
                    idle = False
                    assert_config(config)
                    identifier, action = selected["id"], selected["action"]
                    event("task_started", id=identifier, action=action)
                    if action == "merge":
                        result = merge(config, state, identifier)
                    elif action == "review":
                        result = advance(config, state, identifier)
                    elif action == "submit":
                        result = submit(config, state, identifier)
                    else:
                        result = run_task(config, state, identifier, resume=action == "resume", submit_after=True)
                    if automatic and result["status"] == "merge_waiting":
                        if result.get("merge_gates", {}).get("blockers"):
                            result.update(status="merge_blocked")
                            state.save(result)
                            abort_pending(config, state, result, result.get("error"))
                        else:
                            event("merge_waiting", id=identifier, pr_url=result.get("pr_url"),
                                  reasons=result.get("merge_gates", {}).get("reasons", []),
                                  message=result.get("error"))
                            sleep(poll_interval)
                            continue
                    event("task_finished", id=identifier, status=result["status"], pr_url=result.get("pr_url"))
                    if result["status"] not in (("completed",) if automatic else ("pr_open", "reviewed")) or not result.get("pr_url"):
                        return {"status": "needs_attention", "tasks": [identifier], "delivered": delivered,
                                "message": result.get("error") or "Task did not finish delivery; inspect saved state"}
                    delivered += 1
                    if max_tasks is not None and delivered >= max_tasks:
                        return {"status": "stopped", "reason": "max_tasks", "delivered": delivered}
            except RelayError as error:
                for task in state.tasks():
                    if task.get("merge_request") and not task.get("merge_sha") and task["status"] != "completed":
                        abort_pending(config, state, task, str(error))
                raise
            except KeyboardInterrupt:
                event("watch_stopped", reason="interrupted", delivered=delivered)
                raise
