"""Continuous serial dispatch with configurable manual or verified automatic merge."""

from time import sleep

from .delivery import abort_pending, automatic_review
from .discovery import attach_sources
from .errors import RelayError
from .process import interrupt_on_sigterm
from .providers import queue_report, snapshot
from .runner import assert_config
from .state import now
from .workflow import parkable, work
from .triage import autonomous, automatic, automatic_enabled, authorization_reason, current as current_triage, triage


IMPLEMENTING = {"claimed", "prepared", "preparing", "implementing", "verifying", "interrupted"}
SUBMITTING = {"verified", "committed"}
ATTENTION = {"failed", "blocked", "start_failed", "review_blocked", "merge_blocked",
             "post_merge_failed", "cleanup_failed"}
DELIVERING = {"pr_open", "reviewing", "reviewed", "merge_waiting", "merging", "merged", "cleaning"}


def selection(config, state):
    """Recover the oldest unfinished task before claiming new work."""
    def choose(task, action):
        return {"status": "selected", "id": task["id"], "action": action,
                "automatic": automatic(config, task)}

    tasks = attach_sources(config, state, state.tasks())
    attention = [task["id"] for task in tasks if not parkable(config, task)
                 and (task["status"] in ATTENTION or task.get("handoff"))]
    attention += [task["id"] for task in tasks if automatic(config, task)
                  and (task.get("triage") or {}).get("phase") in ("preparing", "failed")]
    if attention:
        return {"status": "needs_attention", "tasks": attention,
                "message": "Inspect saved errors and explicitly recover these tasks before restarting watch"}
    pending = []
    for task in tasks:
        if parkable(config, task):
            continue
        status = task["status"]
        if task.get("pr_url"):
            if (status in DELIVERING and config.data["delivery"]["auto_merge"]
                    or task.get("merge_request") and status in DELIVERING):
                pending.append((task, "merge"))
            elif status == "reviewing" or status == "pr_open" and automatic_review(config, task):
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
        return choose(task, action)
    view = snapshot(config, state)
    if automatic_enabled(config):
        available = {t["id"]: t for t in queue_report(config, view, exclude_claimed=False, require_authorization=False)["eligible"]}
        for task in tasks:
            fresh = available.get(task["id"])
            if not parkable(config, task) or not fresh:
                continue
            handoff = task["handoff"]
            changed = handoff.get("requirements") != {key: fresh[key] for key in ("title", "body")}
            label = config.data["queue"]["approval_label"]
            changed |= label in fresh["labels"] and label not in handoff.get("labels", task["labels"])
            if changed:
                if not authorization_reason(config, fresh):
                    return choose(fresh, "retry")
                if not current_triage(config, fresh):
                    return choose(fresh, "triage")
    report = queue_report(config, view)
    selected = report["selected"]
    if selected is None:
        candidates = report.get("triage_candidates", [])
        if candidates:
            return choose(candidates[0], "triage")
        return {"status": "idle", "message": "No eligible task; waiting for authorized issues"}
    return choose(selected, "run")


def watch(config, state, *, poll_interval=60, max_tasks=None, reset_budget=False, emit):
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
            budget = None
            if reset_budget and not automatic_enabled(config):
                raise RelayError("--reset-budget requires automatic triage for project Issues or discoveries")
            if automatic_enabled(config):
                budget = state.metadata("watch_budget")
                if reset_budget or budget is None and autonomous(config):
                    if budget:
                        state.set_metadata("watch_budget_history", [*(state.metadata("watch_budget_history") or []), budget])
                    budget = {"used": 0, "active": None, "started_at": now()}
                    state.set_metadata("watch_budget", budget)
            auto_merging = config.data["delivery"]["auto_merge"]
            event("watch_started", poll_interval=poll_interval, max_tasks=max_tasks,
                  mode="auto_merge" if auto_merging else "manual_merge")
            idle = False
            try:
                while True:
                    assert_config(config)
                    if autonomous(config) and budget is not None and not budget["active"] and budget["used"] >= config.data["queue"]["max_dispatches"]:
                        return {"status": "stopped", "reason": "dispatch_budget", "delivered": delivered,
                                "dispatches": budget["used"], "next": "Inspect status, then watch --reset-budget"}
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
                    budgeted = selected["automatic"]
                    if budgeted:
                        if budget is None:
                            budget = {"used": 0, "active": None, "started_at": now()}
                        active = {"id": identifier, "phase": "triage" if action == "triage" else "work"}
                        if budget["active"] != active:
                            if budget["used"] >= config.data["queue"]["max_dispatches"]:
                                return {"status": "stopped", "reason": "dispatch_budget", "delivered": delivered,
                                        "dispatches": budget["used"], "next": "Inspect status, then watch --reset-budget"}
                            budget.update(used=budget["used"] + 1, active=active)
                            state.set_metadata("watch_budget", budget)
                    event("task_started", id=identifier, action=action)
                    if action == "triage":
                        result = triage(config, state, identifier)
                        event("task_triaged", id=identifier, **result["triage"]["result"])
                        budget["active"] = None
                        state.set_metadata("watch_budget", budget)
                        continue
                    try:
                        result = work(config, state, identifier, retry=action == "retry")
                    except RelayError:
                        result = state.get(identifier)
                        assert_config(config)
                        if not result or not parkable(config, result):
                            raise
                    if parkable(config, result):
                        event("task_paused", id=identifier, handoff=result["handoff"])
                        budget["active"] = None
                        state.set_metadata("watch_budget", budget)
                        continue
                    if auto_merging and result["status"] == "merge_waiting":
                        if result.get("merge_gates", {}).get("blockers"):
                            result.update(status="merge_blocked")
                            state.save(result)
                            abort_pending(config, state, result, result.get("error"))
                        elif not result.get("handoff"):
                            event("merge_waiting", id=identifier, pr_url=result.get("pr_url"),
                                  reasons=result.get("merge_gates", {}).get("reasons", []),
                                  message=result.get("error"))
                            sleep(poll_interval)
                            continue
                    event("task_finished", id=identifier, status=result["status"], pr_url=result.get("pr_url"),
                          discoveries=[{key: item.get(key) for key in ("status", "issue_id", "url")}
                                       for item in result.get("discoveries", [])],
                          discovery_error=result.get("discovery_error"))
                    if result.get("handoff") or result["status"] not in (("completed",) if auto_merging else ("pr_open", "reviewed")) or not result.get("pr_url"):
                        return {"status": "needs_attention", "tasks": [identifier], "delivered": delivered,
                                "message": result.get("error") or "Task did not finish delivery; inspect saved state"}
                    delivered += 1
                    if budgeted:
                        budget["active"] = None
                        state.set_metadata("watch_budget", budget)
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
