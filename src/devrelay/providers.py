"""Task snapshots and dependency-aware eligibility; no remote writes."""

from dataclasses import dataclass
import json
import re

from .errors import RelayError
from .git import command, git, is_ancestor, validate_remote, worktrees
from .state import now
from .triage import author_reason, authorization_reason, automatic, automatic_enabled, current as current_triage, identity as triage_identity


ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
PLAN_LABEL = "scope:plan"


def is_tracking_parent(task):
    """Recognize explicit plan markers, including the original DevRelay template."""
    if PLAN_LABEL in {label.casefold() for label in task["labels"]}:
        return True
    return bool(re.search(
        r"^(?:Task kind:[ \t]*plan[ \t]*\r?$|"
        r"##[ \t]+Child issues and delivery plan(?:[ \t]*/[ \t]*子任务与交付计划)?[ \t]*\r?$)",
        task["body"], re.M | re.I,
    ))


def task_id(value):
    value = str(value)
    if not re.fullmatch(ID_PATTERN, value):
        raise RelayError("Task id must use 1-64 letters, digits, hyphens or underscores")
    return value


def dependencies(body, *, github=False):
    lines = re.findall(r"^Depends on:[ \t]*(.*)$", body, re.M)
    if not lines:
        return []
    if len(lines) != 1:
        raise RelayError("Use at most one 'Depends on: none' or 'Depends on: #ID, #ID' line")
    value = lines[0].strip()
    if value == "none":
        return []
    pattern = r"[1-9]\d*" if github else ID_PATTERN
    if not re.fullmatch(rf"#{pattern}(?:,\s*#{pattern})*", value):
        raise RelayError("Malformed dependency declaration")
    identifiers = re.findall(rf"#({pattern})", value)
    if len(set(identifiers)) != len(identifiers):
        raise RelayError("Duplicate dependency")
    return identifiers


def closing_ids(pull, repository):
    identifiers = set()
    for item in pull.get("closingIssuesReferences", []):
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/issues/([1-9]\d*)/?",
                             item.get("url", ""), re.I)
        if (match and match[1].casefold() == repository.casefold()
                and match[2] == str(item.get("number"))):
            identifiers.add(match[2])
    return identifiers


def linked_ids(pull, prefix, repository):
    identifiers = closing_ids(pull, repository)
    branch = pull.get("headRefName", "")
    if branch.startswith(prefix):
        value = branch[len(prefix):]
        if re.fullmatch(ID_PATTERN, value):
            identifiers.add(value)
    for match in re.finditer(r"\b(?:Refs|Fix(?:es|ed)?|Close(?:s|d)?|Resolve(?:s|d)?)\s+(#\d+(?:,\s*#\d+)*)",
                             pull.get("body", ""), re.I):
        identifiers.update(re.findall(r"#(\d+)", match[1]))
    return identifiers


def gh(config, *argv, input=None):
    return command(["gh", *argv, "--repo", f"https://github.com/{config.repo}"], config.root, input=input)


def gh_json(config, *argv):
    try:
        return json.loads(gh(config, *argv))
    except ValueError as error:
        raise RelayError("GitHub CLI returned invalid JSON") from error


@dataclass
class Snapshot:
    tasks: list
    claimed: set
    integrated: set


def paginated_inventory(config, kind):
    """Walk repository connections, avoiding the CLI search inventory ceiling."""
    fields = ("number title body state url createdAt author{login} labels(first:100){nodes{name} pageInfo{hasNextPage}}"
              if kind == "issues" else
              "number headRefName state baseRefName mergeCommit{oid} body url "
              "closingIssuesReferences(first:100){nodes{number url} pageInfo{hasNextPage}}")
    query = ("query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){"
             + kind + "(first:100,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}){nodes{"
             + fields + "} pageInfo{hasNextPage endCursor}}}}")
    owner, name = config.repo.split("/")
    items, seen, cursors, cursor = [], set(), set(), None
    while True:
        argv = ["gh", "api", "--hostname", "github.com", "graphql", "-f", "query=" + query,
                "-f", "owner=" + owner, "-f", "name=" + name]
        if cursor:
            argv += ["-f", "cursor=" + cursor]
        try:
            response = json.loads(command(argv, config.root))
            if response.get("errors"):
                raise ValueError("GraphQL errors")
            connection = response["data"]["repository"][kind]
            nodes, page = connection["nodes"], connection["pageInfo"]
            if not isinstance(nodes, list) or type(page["hasNextPage"]) is not bool:
                raise ValueError("Invalid page")
            for item in nodes:
                number = item["number"]
                if type(number) is not int or number <= 0 or number in seen:
                    raise ValueError("Duplicate or invalid item")
                seen.add(number)
                field = "labels" if kind == "issues" else "closingIssuesReferences"
                nested = item[field]
                if nested["pageInfo"]["hasNextPage"] is not False or not isinstance(nested["nodes"], list):
                    raise ValueError("Truncated item relationships")
                item[field] = nested["nodes"]
                items.append(item)
            if not page["hasNextPage"]:
                return items
            cursor = page["endCursor"]
            if not nodes or not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("Pagination did not advance")
            cursors.add(cursor)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise RelayError(f"GitHub returned incomplete or invalid {kind} inventory; retry the snapshot") from error


def snapshot(config, state, *, ignore_state_claim=None):
    from .discovery import attach_sources
    claimed = {item["id"] for item in state.tasks()
               if (item["status"] != "completed" or config.provider == "github" and item.get("pr_url"))
               and item["id"] != ignore_state_claim
               and not (item["status"] == "queued" and not item.get("worktree"))}
    for tree in worktrees(config.root):
        branch = tree.get("branch", "").removeprefix("refs/heads/")
        if branch.startswith(config.prefix):
            claimed.add(branch[len(config.prefix):])
    if config.provider == "local":
        tasks = state.tasks()
        integrated = {item["id"] for item in tasks if item["status"] == "completed"
                      and item.get("head_sha") and is_ancestor(config.root, item["head_sha"], config.base_ref)}
        return Snapshot(attach_sources(config, state, tasks), claimed, integrated)
    validate_remote(config)
    git(config.root, "fetch", config.remote, config.base)
    issues = gh_json(config, "issue", "list", "--state", "all", "--limit", "1000", "--json",
                     "number,title,body,state,labels,url,createdAt,author")
    pulls = gh_json(config, "pr", "list", "--state", "all", "--limit", "1000", "--json",
                    "number,headRefName,state,baseRefName,mergeCommit,closingIssuesReferences,body,url")
    if len(issues) >= 1000:
        issues = paginated_inventory(config, "issues")
    if len(pulls) >= 1000:
        pulls = paginated_inventory(config, "pullRequests")
    integrated = set()
    for pull in pulls:
        if pull["state"] == "OPEN":
            claimed.update(linked_ids(pull, config.prefix, config.repo))
        oid = (pull.get("mergeCommit") or {}).get("oid", "")
        if (pull["state"] == "MERGED" and pull["baseRefName"] == config.base
                and re.fullmatch(r"[0-9a-f]{40,64}", oid)
                and is_ancestor(config.root, oid, config.base_ref)):
            integrated.update(closing_ids(pull, config.repo))
    tasks = [{"id": str(item["number"]), "title": item["title"], "body": item["body"] or "",
              "open": item["state"] == "OPEN", "labels": [label["name"] for label in item["labels"]],
              "created_at": item["createdAt"], "url": item["url"],
              "author": (item.get("author") or {}).get("login")} for item in issues]
    saved = {item["id"]: item for item in state.tasks()}
    for task in tasks:
        if saved.get(task["id"], {}).get("triage"):
            task["triage"] = saved[task["id"]]["triage"]
    return Snapshot(attach_sources(config, state, tasks), claimed, integrated)


def queue_report(config, view, *, exclude_claimed=True, require_authorization=True):
    queue = config.data["queue"]
    by_id = {item["id"]: item for item in view.tasks}
    declarations, validation = {}, {}

    def declaration(identifier):
        if identifier not in declarations:
            item = by_id.get(identifier)
            if item is None:
                declarations[identifier] = ([], [f"Unknown dependency #{identifier}"])
            else:
                try:
                    declarations[identifier] = (dependencies(item["body"], github=config.provider == "github"), [])
                except RelayError as error:
                    historical = not item.get("open", True) and not re.search(r"^Depends on:", item["body"], re.M)
                    declarations[identifier] = ([], [] if historical else [str(error)])
        return declarations[identifier]

    def dependency_errors(identifier, trail=()):
        if len(trail) > 64:
            return ["Dependency chain exceeds the supported depth (64)"]
        if identifier in trail:
            cycle = (*trail[trail.index(identifier):], identifier)
            return ["Dependency cycle: " + " -> ".join(cycle)]
        key = (identifier, len(trail))
        if key not in validation:
            required, errors = declaration(identifier)
            errors = list(errors)
            for dependency in required:
                errors.extend(dependency_errors(dependency, (*trail, identifier)))
                target = by_id.get(dependency)
                if target and (target.get("open", True) or dependency not in view.integrated):
                    errors.append(f"Dependency #{dependency} is not closed and integrated into the base")
            validation[key] = tuple(dict.fromkeys(errors))
        return validation[key]

    eligible, excluded, investigations = [], [], []
    for task in view.tasks:
        if not task.get("open", True):
            continue
        labels = set(task["labels"])
        reasons = []
        author = author_reason(config, task)
        if author:
            reasons.append(author)  # Investigation also enforces authors, before authorization exists.
        if is_tracking_parent(task):
            reasons.append("Tracking plan parents are not executable; authorize independently deliverable child issues")
        reasons.extend(label for label in queue["blockers"] if label in labels)
        if len(labels.intersection(queue["priorities"])) > 1:
            reasons.append("At most one priority label is allowed")
        if not task.get("created_at"):
            reasons.append("Creation time is missing")
        reasons.extend(dependency_errors(task["id"]))
        if exclude_claimed and task["id"] in view.claimed:
            reasons.append("Existing work or PR; resume it instead of claiming again")
        authorization = authorization_reason(config, task) if require_authorization else None
        record = task.get("triage") or {}
        if (not reasons and authorization and automatic(config, task) and not current_triage(config, task)
                and (record.get("identity") != triage_identity(config, task) or record.get("phase") == "done")):
            investigations.append(task)
        if authorization:
            reasons.append(authorization)
        if reasons:
            excluded.append({"id": task["id"], "reasons": list(dict.fromkeys(reasons))})
        else:
            eligible.append(task)
    def order(item):
        return (next((i for i, label in enumerate(queue["priorities"]) if label in item["labels"]),
                     min(1, len(queue["priorities"]) - 1)), item["created_at"], item["id"])
    eligible.sort(key=order)
    investigations.sort(key=order)
    result = {"selected": eligible[0] if eligible else None, "eligible": eligible, "excluded": excluded}
    if automatic_enabled(config):
        result["triage_candidates"] = investigations
    return result


def authorized(config, state, identifier):
    identifier = task_id(identifier)
    view = snapshot(config, state)
    report = queue_report(config, view, exclude_claimed=False)
    for task in report["eligible"]:
        if task["id"] == identifier:
            return task
    reasons = next((item["reasons"] for item in report["excluded"] if item["id"] == identifier), ["Task is missing or closed"])
    raise RelayError(f"Task {identifier} is not eligible: {'; '.join(reasons)}")


def add_local(config, state, identifier, title, body, priority):
    if config.provider != "local":
        raise RelayError("task add is for provider=local; create GitHub Issues for provider=github")
    identifier = task_id(identifier)
    if state.get(identifier):
        raise RelayError(f"Task {identifier} already exists; left unchanged")
    if not title.strip() or not body.strip():
        raise RelayError("Provide a task title and a nonempty task file")
    if priority is not None and priority not in config.data["queue"]["priorities"]:
        raise RelayError("Priority must match a configured queue priority label")
    if not re.search(r"^Depends on:", body, re.M):
        body = body.rstrip() + "\n\nDepends on: none\n"
    dependencies(body)
    return state.save({"id": identifier, "title": title, "body": body, "open": True,
                       "labels": [config.data["queue"]["approval_label"]] + ([priority] if priority else []),
                       "created_at": now(), "status": "queued", "attempts": 0})


def approve_local(config, state, identifier):
    if config.provider != "local":
        raise RelayError("task approve is for provider=local; apply the configured approval label on GitHub")
    task = state.get(task_id(identifier))
    if not task or task["status"] != "queued" or task.get("worktree") or not task.get("open", True):
        raise RelayError("Only an open, unclaimed local task can be approved")
    if is_tracking_parent(task):
        raise RelayError("Tracking plan parents are not executable; authorize bounded child tasks")
    label = config.data["queue"]["approval_label"]
    if label not in task["labels"]:
        task["labels"].append(label)
    return state.save(task)
