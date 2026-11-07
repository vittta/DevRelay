"""Evidence-backed follow-up intake, shared by external and managed agents."""

import hashlib

from . import providers
from .errors import RelayError
from .git import git, source_tree, validate_remote
from .state import now


FIELDS = ("title", "description", "evidence", "acceptance")
REPORT_SCHEMA = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in FIELDS},
    "required": list(FIELDS),
    "additionalProperties": False,
}


def result_schema(schema, config):
    if not config.data["discovery"]["enabled"]:
        return schema
    return {**schema, "properties": {**schema["properties"], "discoveries": {
        "type": "array", "items": REPORT_SCHEMA,
        "maxItems": config.data["discovery"]["max_per_task"],
    }}, "required": [*schema["required"], "discoveries"]}


def instructions(config):
    if not config.data["discovery"]["enabled"]:
        return ""
    return """
Report independently actionable, out-of-scope bugs you actually discover in
discoveries (an empty array when none). Each needs title, description, evidence
(reproduction steps, expected/actual behavior and observed results or precise code
evidence), and acceptance (a bounded observable result). Check existing issues when
available and reuse their titles for known duplicates. Do not invent findings or
start an unrelated audit. Keep credentials and private data out of reports.
Regressions introduced here, missing acceptance and unfinished requirements belong
to THIS task, never to a follow-up that excuses a failed check or review. Report
pre-existing independent bugs separately; they do not change this task's verdict.
Do not create issues or grant authorization yourself. The controller records and
deduplicates these reports, then publishes follow-ups under project policy (manual
approval or an independent automatic investigation). Reporting a
finding does not verify it or authorize its implementation.
"""


def validate(reports):
    if not isinstance(reports, list) or len(reports) > 20:
        raise RelayError("discoveries must be an array of at most 20 reports")
    for report in reports:
        if not isinstance(report, dict) or set(report) != set(FIELDS):
            raise RelayError("Each discovery needs title, description, evidence and acceptance")
        for field, value in report.items():
            if (not isinstance(value, str) or not value.strip() or "\x00" in value
                    or len(value) > (200 if field == "title" else 8000)
                    or "<!-- devrelay-discovery:" in value):
                raise RelayError(f"Invalid discovery {field}")
        if "\n" in report["title"] or "\r" in report["title"]:
            raise RelayError("Discovery title must be one line")
        if providers.is_tracking_parent({"labels": [], "body": "\n".join(report.values())}):
            raise RelayError("A discovery must be a bounded issue, not a tracking plan")
    return reports


def title_key(title):
    return " ".join(title.casefold().split())


def record(config, state, task, reports, *, observation):
    """Persist the complete batch before any remote mutation; never change acceptance."""
    reports = validate(reports)
    if not reports:
        return task
    entries = task.setdefault("discoveries", [])
    by_key = {entry["key"]: entry for entry in entries}
    keyed = [(hashlib.sha256(title_key(report["title"]).encode()).hexdigest(), report) for report in reports]
    if len(set(by_key) | {key for key, _ in keyed}) > config.data["discovery"]["max_per_task"]:
        raise RelayError("Discovery limit for this task reached; inspect reports before changing discovery.max_per_task")
    for key, report in keyed:
        if key not in by_key:
            by_key[key] = {"key": key, "report": report, "status": "recorded", "observations": []}
            entries.append(by_key[key])
        entry = by_key[key]
        evidence = {**observation, "report": report}
        if evidence not in entry["observations"]:
            entry["observations"].append(evidence)
    state.save(task)
    return task


def intake(config, state, task, reports=None, *, origin=None, report_path=None):
    if reports:
        validate(reports)
        batch = {"reports": reports, "observation": {
            "origin": origin, "report_path": str(report_path) if report_path else None,
            "provider": config.provider, "repository": config.repo,
            "head_sha": git(task["worktree"], "rev-parse", "HEAD"),
            "source_tree": source_tree(task["worktree"]),
        }}
        pending = task.setdefault("discovery_pending", [])
        if batch not in pending:
            pending.append(batch)
        state.save(task)  # Keep valid reports and their original source even if the limit is reached.
    for batch in list(task.get("discovery_pending", [])):
        record(config, state, task, batch["reports"], observation=batch["observation"])
        task["discovery_pending"].remove(batch)
        state.save(task)


def issue_body(task, entry):
    report, observed = entry["report"], entry["observations"][0]
    source = task.get("url") or f"task {task['id']}"
    return (f"## Problem\n\n{report['description']}\n\n"
            f"## Evidence\n\n{report['evidence']}\n\n"
            f"## Acceptance\n\n{report['acceptance']}\n\n"
            f"## Discovery source\n\nReported during {source} ({observed['origin']}).\n"
            f"Observed HEAD: `{observed['head_sha']}`\n\n"
            f"Observed source tree: `{observed['source_tree']}`\n\n"
            "Agent-reported evidence; not independent verification or execution authorization.\n\n"
            f"<!-- devrelay-discovery:{entry['key']} -->\n")


def inventory(config, state):
    if config.provider == "local":
        return [{"number": item["id"], "title": item["title"], "body": item["body"],
                 "url": item.get("url"), "state": "OPEN" if item.get("open", True) else "CLOSED"}
                for item in state.tasks()]
    validate_remote(config)
    issues = providers.gh_json(config, "issue", "list", "--state", "all", "--limit", "1000", "--json",
                               "number,title,body,state,url")
    if not isinstance(issues, list):
        raise RelayError("GitHub returned invalid discovery inventory")
    if len(issues) >= 1000:
        issues = providers.paginated_inventory(config, "issues")
    seen = set()
    for item in issues:
        if (not isinstance(item, dict) or type(item.get("number")) is not int or item["number"] <= 0
                or item["number"] in seen or not isinstance(item.get("title"), str)
                or not isinstance(item.get("body"), (str, type(None)))
                or item.get("state") not in ("OPEN", "CLOSED")
                or item.get("url") != f"https://github.com/{config.repo}/issues/{item['number']}"):
            raise RelayError("GitHub returned incomplete or invalid discovery inventory")
        seen.add(item["number"])
    return issues


def match_issue(entry, issues):
    marker = f"<!-- devrelay-discovery:{entry['key']} -->"
    marked = [item for item in issues if marker in (item["body"] or "")]
    matches = marked or [item for item in issues if title_key(item["title"]) == title_key(entry["report"]["title"])]
    if len(matches) > 1:
        raise RelayError("Multiple issues match this discovery; reconcile duplicates before retrying")
    return matches[0] if matches else None


def attach_sources(config, state, tasks):
    """Trust controller publication receipts, never an Issue's claimed author or label."""
    by_id = {task["id"]: task for task in tasks}
    for task in tasks:
        task.pop("discovery_source", None)
    for source in state.tasks():
        for entry in source.get("discoveries", []):
            task = by_id.get(entry.get("issue_id"))
            observations = entry.get("observations", [])
            if not task or entry["status"] != "published" or not observations:
                continue
            observed = observations[0]
            if ((observed["provider"], observed["repository"]) == (config.provider, config.repo)
                    and f"<!-- devrelay-discovery:{entry['key']} -->" in task["body"]):
                task["discovery_source"] = {"id": source["id"], "key": entry["key"],
                                            "provider": config.provider, "repository": config.repo}
    return tasks


def publish(config, state, task, *, retry_create=False):
    """Reconcile uncertain writes before retrying; creating an issue never approves it."""
    from .runner import assert_config
    entries = [entry for entry in task.get("discoveries", []) if entry["status"] in ("recorded", "publishing")]
    if not entries:
        task.pop("discovery_error", None)
        return state.save(task)
    issues = inventory(config, state)
    for entry in entries:
        observed = entry["observations"][0]
        if (observed["provider"], observed["repository"]) != (config.provider, config.repo):
            raise RelayError("Discovery belongs to a different project; restore its original configuration")
        existing = match_issue(entry, issues)
        if existing is None:
            if entry["status"] == "publishing" and not retry_create:
                raise RelayError("Discovery creation outcome is uncertain; inspect GitHub, then use discover ID --retry-create if absent")
            assert_config(config)
            providers.authorized(config, state, task["id"])
            assert_config(config)
            body = issue_body(task, entry)
            if config.provider == "local":
                identifier = "found-" + entry["key"][:20]
                if state.get(identifier):
                    raise RelayError(f"Discovery task id {identifier} already exists; left unchanged")
                state.save({"id": identifier, "title": entry["report"]["title"], "body": body,
                            "open": True, "labels": [], "status": "queued", "attempts": 0,
                            "created_at": now(), "discovered_from": task["id"]})
            else:
                entry["status"] = "publishing"
                state.save(task)  # A lost response must not cause another issue creation.
                providers.gh(config, "issue", "create", "--title", entry["report"]["title"],
                             "--body-file", "-", input=body)
            issues = inventory(config, state)
            existing = match_issue(entry, issues)
            if existing is None:
                raise RelayError("Created discovery is not visible yet; run discover ID to reconcile")
            disposition = "published"
        else:
            marker = f"<!-- devrelay-discovery:{entry['key']} -->"
            disposition = "published" if entry["status"] == "publishing" and marker in (existing["body"] or "") else "existing"
        entry.update(status=disposition, issue_id=str(existing["number"]), url=existing.get("url"))
        state.save(task)
        if config.provider == "local" and disposition == "published":
            child = state.get(entry["issue_id"])
            state.save(attach_sources(config, state, [child])[0])
    task.pop("discovery_error", None)
    return state.save(task)


def capture(config, state, task, result, *, origin, report_path):
    """A follow-up publication failure must not erase or excuse the original result."""
    if not config.data["discovery"]["enabled"]:
        return task
    try:
        intake(config, state, task, result.get("discoveries", []), origin=origin, report_path=report_path)
        return publish(config, state, task)
    except (RelayError, OSError) as error:
        task["discovery_error"] = {"message": str(error), "report_path": str(report_path),
                                   "next": f"devrelay discover {task['id']}"}
        return state.save(task)


def discover(config, state, identifier, reports=None, *, retry_create=False, report_path=None):
    from .runner import claimed_task
    task = (claimed_task(config, state, identifier) if reports is not None
            else state.get(providers.task_id(identifier)))
    if not task:
        raise RelayError(f"Unknown source task {identifier}")
    try:
        if reports is not None:
            validate(reports)
        intake(config, state, task, reports, origin="external-agent", report_path=report_path)
        return publish(config, state, task, retry_create=retry_create)
    except (RelayError, OSError) as error:
        task["discovery_error"] = {"message": str(error), "next": f"devrelay discover {identifier}"}
        state.save(task)
        raise
