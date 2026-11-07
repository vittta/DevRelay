"""JSON engineering tools for agents, with optional Codex orchestration."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__
from .config import initialize, load
from .delivery import merge, review
from .discovery import attach_sources, discover
from .errors import RelayError
from .git import has_ref, repository_root, validate_remote, worktrees
from .onboarding import setup_github
from .process import interrupt_on_sigterm
from .providers import add_local, queue_report, snapshot
from .runner import complete_local, prepare_task, run_task, start, submit, verify
from .state import State
from .worker import ATTENTION, watch
from .workflow import guidance, work
from .triage import automatic_enabled, triage


def parser():
    root = argparse.ArgumentParser(
        prog="devrelay", description="Agent-driven engineering tools with verified delivery gates",
        epilog="Agents use next/start/prepare/check/submit. Optional task continuation: work/watch (run/resume compatible).")
    root.add_argument("-V", "--version", action="version", version=__version__)
    locations = root.add_mutually_exclusive_group()
    locations.add_argument("--workspace", type=Path, help="Explicit multi-repository TOML manifest (draft delivery only)")
    locations.add_argument("--project", type=Path, help="Project directory (task commands default to current directory)")
    sub = root.add_subparsers(dest="command", required=True)
    ui = sub.add_parser("ui", help="Open a local configuration dashboard; does not start a worker")
    ui.add_argument("--catalog", type=Path, help="TOML project catalog (separate from a task workspace manifest)")
    ui.add_argument("--port", type=int, default=8765, help="Loopback HTTP port (default: 8765; 0 selects a free port)")
    ui.add_argument("--open", action="store_true", help="Open the dashboard in your default browser")
    catalog = sub.add_parser("projects", help="Register local repositories for the dashboard; does not start tasks")
    catalog.add_argument("--catalog", type=Path, help="Override the user project catalog")
    project_commands = catalog.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list", help="List registered repositories")
    add_project = project_commands.add_parser("add", help="Keep or create project configuration and register its controller")
    add_project.add_argument("path", type=Path)
    add_project.add_argument("--name", default="", help="Project display name")
    relocate = project_commands.add_parser("locate", help="Relocate an existing project after checking repository identity")
    relocate.add_argument("id")
    relocate.add_argument("path", type=Path)
    init = sub.add_parser("init", help="Create project configuration and initialize GitHub issue management")
    init.add_argument("--provider", choices=("local", "github"))
    init.add_argument("--mode", choices=("manual", "autonomous"), help="Project authorization mode (default: manual)")
    init.add_argument("--base", help="Target branch; detected from origin/HEAD, main, master or current branch")
    init.add_argument("--sync-github", action="store_true",
                      help="Add missing GitHub labels/templates using existing configuration; do not overwrite it")
    diagnostics = sub.add_parser("doctor", help="Check engineering tools and configured delivery requirements")
    diagnostics.add_argument("--with-codex", action="store_true",
                             help="Also check Codex CLI/login for optional run/resume/watch orchestration")
    sub.add_parser("next", help="Explain eligible tasks and exclusions; does not claim work")
    sub.add_parser("status", help="Read local task state and evidence locations")
    task = sub.add_parser("task", help="Manage explicit local tasks").add_subparsers(dest="task_command", required=True)
    add = task.add_parser("add", help="Add and approve one local task")
    add.add_argument("id")
    add.add_argument("--title", required=True)
    add.add_argument("--file", required=True, type=Path)
    add.add_argument("--priority", help="Optional configured priority label; defaults to normal priority")
    task.add_parser("revoke", help="Revoke a workspace task; preserve its work and evidence").add_argument("id")
    task.add_parser("list", help="List local task state")
    task.add_parser("complete", help="Mark a local task complete after integration into the base").add_argument("id")
    task.add_parser("approve", help="Explicitly authorize a local discovered task").add_argument("id")
    investigation = sub.add_parser("triage", help="Investigate a task under project policy before automatic execution")
    investigation.add_argument("id")
    intake = investigation.add_mutually_exclusive_group()
    intake.add_argument("--prepare", action="store_true", help="Prepare isolated checkout, prompt and schema for an external agent")
    intake.add_argument("--file", type=Path, help="Accept an external agent's result bound to the prepared context")
    investigation.add_argument("--retry", action="store_true", help="Start a fresh investigation, retaining earlier evidence")
    discovery = sub.add_parser("discover", help="Record follow-ups for manual authorization or automatic triage")
    discovery.add_argument("id", help="Source task ID")
    discovery.add_argument("--file", type=Path, help="JSON array of title/description/evidence/acceptance reports")
    discovery.add_argument("--retry-create", action="store_true", help="Retry an uncertain issue creation only after checking GitHub for duplicates")
    for name in ("start", "prepare", "check", "submit", "resume", "review", "merge"):
        item = sub.add_parser(name, help={"start": "Claim a task and create or recover its worktree",
                                         "prepare": "Run project setup in a claimed worktree without invoking a model",
                                         "check": "Run configured acceptance and save exact-source evidence",
                                         "submit": "Commit verified changes and create/update a draft PR",
                                         "resume": "Optional orchestration: continue a saved Codex implementation session",
                                         "review": "Review a submitted PR in a fresh read-only Codex session",
                                         "merge": "Apply enabled merge gates and verify the merged commit"}[name])
        item.add_argument("id")
        if name == "submit":
            item.add_argument("--local", action="store_true", help="Create a local commit without push or PR")
        if name == "merge":
            item.add_argument("--retry-request", action="store_true",
                              help="Explicitly retry a saved merge request only after remote queue/auto-merge is absent")
        if name == "resume":
            item.add_argument("--submit", action="store_true", help="Submit a draft PR after checks pass")
    run = sub.add_parser("run", help="Optional orchestration: invoke Codex to implement and verify one task")
    run.add_argument("id", nargs="?")
    run.add_argument("--submit", action="store_true", help="Submit a draft PR after checks pass")
    continuation = sub.add_parser("work", help="Continue a task through checks, bounded repair and configured delivery")
    continuation.add_argument("id", nargs="?", help="Task to continue; otherwise recover pending work or select an authorized task")
    continuation.add_argument("--retry", action="store_true", help="After resolving a handoff, explicitly start another bounded attempt budget")
    worker = sub.add_parser("watch", help="Optional orchestration: serial Codex tasks through configured delivery policy")
    worker.add_argument("--poll-interval", "--interval", type=positive_integer, default=60,
                        help="Seconds between empty-queue and merge-gate polls (default: 60)")
    worker.add_argument("--max-tasks", "--limit", type=positive_integer,
                        help="Stop after this many deliveries (post-merge acceptance in auto mode; default: keep watching)")
    worker.add_argument("--reset-budget", action="store_true",
                        help="Explicitly renew autonomous dispatch budget; ordinary restarts retain usage")
    return root


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def probe(argv, cwd):
    try:
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def doctor(config, *, with_codex=False):
    executable = config.data["codex"]["executable"]
    checks = [{"id": "git", "ok": shutil.which("git") is not None}]
    try:
        base_ref = config.base_ref
        checks.append({"id": "base", "ok": has_ref(config.root, base_ref), "value": base_ref})
    except RelayError as error:
        checks.append({"id": "base", "ok": False, "error": str(error)})
    checks += [{"id": "acceptance", "ok": bool(config.data["checks"]), "count": len(config.data["checks"])},
               {"id": "git_identity", "ok": probe(["git", "var", "GIT_AUTHOR_IDENT"], config.root)}]
    codex_required = (with_codex or config.data["delivery"]["auto_review"] or config.data["delivery"]["auto_merge"]
                      or config.provider == "github" and config.data["discovery"]["auto_review"])
    if codex_required:
        checks += [{"id": "codex", "ok": shutil.which(executable) is not None},
                   {"id": "codex_login", "ok": probe([executable, "login", "status"], config.root)}]
    if config.provider == "github":
        try:
            validate_remote(config)
            matches = True
        except RelayError:
            matches = False
        # Check the credentials gh actually uses; auth status also checks inactive accounts.
        checks += [{"id": "gh", "ok": shutil.which("gh") is not None},
                   {"id": "github_login", "ok": probe(["gh", "api", "--hostname", "github.com", "user", "--silent"], config.root)},
                   {"id": "remote_matches_repository", "ok": matches}]
    from .project_registry import registered_controller, runtime_info
    registered = registered_controller(config.root)
    checks.append({"id": "controller", "ok": registered == config.root.resolve(),
                   "value": str(registered) if registered else "Run devrelay projects add PATH to register this checkout"})
    return {"runtime": runtime_info(), "ready": all(item["ok"] for item in checks), "provider": config.provider,
            "checks": checks, "codex_required": codex_required,
            "notes": ["Use doctor --with-codex before optional run/resume/watch or explicit AI review.",
                      "Login checks do not call a model or verify model access.",
                                         "Review setup and acceptance commands before execution."]}


def controller(root, state):
    saved = state.metadata("controller_root")
    if not saved:
        return root
    path = Path(saved)
    if not path.is_dir() or not any(Path(item["worktree"]).resolve() == path.resolve() for item in worktrees(root)):
        raise RelayError("Saved controller checkout moved or disappeared; restore it before running tasks")
    return path


def main(argv=None):
    args = parser().parse_args(argv)
    with interrupt_on_sigterm():
        return _dispatch(args)


def result_exit_code(result):
    attention = result.get("status") in ATTENTION | {"needs_attention"}
    blocked_merge = (result.get("status") == "merge_waiting"
                     and bool(result.get("merge_gates", {}).get("blockers")))
    return 1 if attention or blocked_merge or result.get("handoff") or result.get("ready") is False else 0


def _dispatch(args):
    state = None
    try:
        if args.command == "ui":
            if args.workspace:
                raise RelayError("ui uses --catalog for independent projects, not --workspace")
            from .ui import serve
            return serve(args.project, catalog=args.catalog, port=args.port, open_browser=args.open)
        if args.command == "projects":
            if args.workspace or args.project:
                raise RelayError("projects takes its own repository path; omit --workspace and --project")
            from .project_registry import Registry
            registry = Registry(args.catalog)
            if args.project_command == "list":
                result = {"catalog": str(registry.path), "projects": registry.read()[1]}
            else:
                result = registry.add(str(args.path), name=getattr(args, "name", ""),
                                      replace_id=args.id if args.project_command == "locate" else None)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.workspace:
            from .workspace import dispatch
            result = dispatch(args)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return result_exit_code(result)
        if args.command == "task" and args.task_command == "revoke":
            raise RelayError("task revoke requires --workspace")
        root = repository_root(args.project or Path.cwd())
        state = State(root)
        root = controller(root, state)
        if args.command == "init":
            with state.lock():
                if args.sync_github:
                    if args.provider or args.base or args.mode:
                        raise RelayError("--sync-github uses existing configuration; omit --provider and --base, and omit --mode")
                    config = load(root)
                    result = {"config": str(root / ".devrelay/config.toml"), "provider": config.provider}
                else:
                    result = initialize(root, args.provider, args.base, mode=args.mode or "manual")
                    config = load(root)
                state.set_metadata("controller_root", str(root))
                if config.provider == "github" or args.sync_github:
                    result["github"] = setup_github(config)
        else:
            config = load(root)
            if args.command == "doctor":
                result = doctor(config, with_codex=args.with_codex)
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return result_exit_code(result)
            elif args.command in ("status", "task") and (args.command == "status" or args.task_command == "list"):
                result = {"project": str(root), "tasks": [{**task, "next_action": guidance(config, task)}
                          for task in attach_sources(config, state, state.tasks())]}
                if automatic_enabled(config):
                    result.update(mode=config.data["queue"]["mode"], discovery_policy={
                                      key: config.data["discovery"][key] for key in ("auto_triage", "auto_review")},
                                  dispatch_budget=state.metadata("watch_budget"),
                                  max_dispatches=config.data["queue"]["max_dispatches"])
            elif args.command == "next":
                result = queue_report(config, snapshot(config, state))
            elif args.command == "watch":
                result = watch(config, state, poll_interval=args.poll_interval, max_tasks=args.max_tasks,
                               reset_budget=args.reset_budget,
                               emit=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return result_exit_code(result)
            else:
                with state.lock():
                    if args.command == "task" and args.task_command == "add":
                        result = add_local(config, state, args.id, args.title, args.file.read_text(), args.priority)
                    elif args.command == "task" and args.task_command == "approve":
                        from .providers import approve_local
                        result = approve_local(config, state, args.id)
                    elif args.command == "task":
                        result = complete_local(config, state, args.id)
                    elif args.command == "discover":
                        try:
                            reports = json.loads(args.file.read_text()) if args.file else None
                        except ValueError as error:
                            raise RelayError("Discovery file must be a JSON array") from error
                        result = discover(config, state, args.id, reports, retry_create=args.retry_create, report_path=args.file)
                    elif args.command == "triage":
                        if args.file and args.retry:
                            raise RelayError("Use triage --prepare --retry first, then submit the new context's result with --file")
                        try:
                            report = json.loads(args.file.read_text()) if args.file else None
                        except ValueError as error:
                            raise RelayError("Triage file must be a JSON object") from error
                        result = triage(config, state, args.id, prepare_only=args.prepare, report=report, retry=args.retry)
                    elif args.command == "start":
                        result = start(config, state, args.id)
                    elif args.command == "prepare":
                        result = prepare_task(config, state, args.id)
                    elif args.command == "check":
                        result = verify(config, state, args.id)
                    elif args.command == "submit":
                        result = submit(config, state, args.id, local=args.local)
                    elif args.command == "review":
                        result = review(config, state, args.id)
                    elif args.command == "merge":
                        result = merge(config, state, args.id, retry_request=args.retry_request)
                    elif args.command == "work":
                        if args.retry and not args.id:
                            raise RelayError("work --retry requires a task ID")
                        result = work(config, state, args.id, retry=args.retry)
                    else:
                        result = run_task(config, state, args.id, resume=args.command == "resume", submit_after=args.submit)
        if isinstance(result, dict) and "id" in result and "status" in result:
            result = {**result, "next_action": guidance(config, result)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if args.command == "discover" and result.get("discovery_error") else result_exit_code(result)
    except (RelayError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": "Interrupted; saved work is retained. Inspect devrelay status; "
                                   "retry the engineering tool, or resume a DevRelay-managed Codex task."}), file=sys.stderr)
        return 130
    finally:
        if state:
            state.close()
