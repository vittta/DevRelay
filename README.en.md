# DevRelay

English | [简体中文](README.md)

General engineering tools for agents: agents plan, explore and implement, while DevRelay provides isolated worktrees, project acceptance and gated delivery.

The primary workflow is driven by an external agent calling `next/start/prepare/check/submit`; DevRelay records engineering state and verification evidence. `run/resume/watch` remain optional Codex orchestration. Projects own setup commands and acceptance rules. Version 0.4 supports macOS / Linux, Python 3.11+ and Git, with no third-party runtime dependencies.

## Quick usage guide (GitHub projects)

The everyday flow is: **describe a task → maintainer approves it → an agent implements it → review and merge the PR**. The examples use Issue `#12`; replace it with your issue number. Run terminal commands from the target project's main checkout (your usual repository directory).

### 1. Set up the project

Follow [installation](#install-and-onboard) once and sign in to GitHub CLI (`gh`). Built-in Codex orchestration and AI review also require an installed, signed-in Codex CLI.

```bash
devrelay ui --open
```

Choose **添加项目** (Add project), enter a local repository path, check it and confirm. Existing `.devrelay/config.toml` files are preserved; missing configuration is generated for review. One Git repository has one registered controller; its other worktrees share that entry.

Review setup and acceptance under **项目验收**, including at least one real acceptance command. Under **状态**, explicitly initialize GitHub labels/templates and run the environment check. Registration, GitHub initialization and diagnostics do not start tasks or run project setup commands.

Commit configuration, ignore rules and templates through the project's PR process. Templates appear on GitHub after reaching its default branch. A fresh clone with existing configuration still needs local project registration on the new machine.

### 2. Create a GitHub Issue and add labels

Open **Issues → New issue** and describe the current problem, the expected result and how to verify completion. For example: “An empty filename crashes export; prompt the user to enter a filename and cover this behavior with a test.” One Issue should describe one verifiable outcome; its code, tests and documentation can stay together.

Set an author allowlist under `[queue]` in `.devrelay/config.toml` to handle only GitHub Issues created by you or your autodev account:

```toml
[queue]
allowed_authors = ["your-github-login", "your-autodev-login"]
```

Use actual GitHub logins; matching is case-insensitive. List the account once if autodev uses your login. Omitting the field or using `[]` preserves unrestricted authors; local tasks are unaffected. This repository uses `["vittta"]` for both its maintainer and autodev sharing that account. Unlisted or unknown authors cannot enter investigation, execution or delivery, even with an approval label, autonomous mode or discovery provenance. `next` explains exclusions; recovery and publication recheck authors while preserving existing work and evidence. Other authors' Issues remain readable for dependencies and deduplication.

After passing the author restriction, manually created Issues in the default `manual` mode still need a maintainer to review them and add `auto:approved` using **Labels** on the right. Common default labels are:

| Label | When to use it |
|---|---|
| `bug` / `enhancement` / `documentation` / `experiment` / `infrastructure` | Classify fixes, improvements, documentation, experiments or infrastructure; classification does not authorize execution |
| `auto:approved` | A maintainer authorizes this task |
| `priority:P0` / `priority:P1` / `priority:P2` | Highest to lowest priority; select at most one; no label means P1 |
| `blocked` / `needs-input` | Pause a task with a blocker or missing information |
| `scope:plan` | Track an overall plan and its children; never execute the parent |

If a goal contains multiple independently deliverable tasks, track them in a plan parent and authorize the concrete child Issues separately. The parent cannot execute even with `auto:approved`; a standalone `Task kind: plan` line in its body also marks it as a plan. Ordinary tasks do not need splitting just to use DevRelay.

**Missing these options in the label dropdown?** Run:

```bash
devrelay init --sync-github
```

This creates missing labels directly on GitHub and missing templates locally, preserving existing content. GitHub labels are repository settings; `git push` does not create them. Template files need to be committed and pushed.

This is the default flow for human-created Issues. [Autonomous mode](#autonomous-projects) can investigate them and execute those within scope; [tasks discovered and created through DevRelay](#agent-discovered-follow-ups) also default to investigation before execution.

### 3. Start work

The primary approach is to have your existing agent call DevRelay. You can tell it:

> Use DevRelay to handle Issue #12: first confirm it is eligible, then claim it, prepare the environment and implement in the returned worktree. Run acceptance, repair failures and rerun checks, then create a PR once they pass.

See the [primary workflow](#primary-workflow-agent-driven) for the agent's commands. To have DevRelay launch Codex itself, first run `devrelay doctor --with-codex`; once checks pass, choose one option:

```bash
devrelay work 12  # Handle this task through the configured delivery stages.
# Or:
devrelay watch    # Keep polling the queue and handle eligible tasks in order.
```

Configuration and labels do not start an executor on their own. Use only one executor per repository; do not start `work` / `watch` while an external agent is working there.

### 4. Check results and resolve blocked work

```bash
devrelay status  # Read progress, PR URLs, errors and next_action.
devrelay next    # See eligible Issues and reasons others are excluded.
```

If work has not started, check authorization, blocker labels and dependencies. Add missing information to the Issue **body**, not just a comment, and remove the relevant blocker labels after resolving the problem. For tasks handled by `work/watch`, follow `next_action`; use `devrelay work 12 --retry` when a fresh attempt budget is needed. Let the original external agent continue tasks it handles.

Passing acceptance leads to a new or updated PR, which defaults to a draft. **Automatic merging is off by default**: inspect the changes and checks on GitHub, then merge when satisfied. Automatic merging requires separate configuration. Detailed recovery and delivery rules follow below.

## Local configuration dashboard

Start the dashboard from any directory (Python 3.11+, no extra dependencies):

```bash
devrelay ui --open
# Or select a project and port
devrelay --project /path/to/project ui --port 8765
```

Open the printed `http://127.0.0.1:8765` address. Use `--port 0` for an available port; Ctrl-C stops the server.
The dashboard currently has a Chinese interface. Edit issue intake, agent-discovery policy, model/timeouts/attempt budgets,
review and merge policy, project identity, setup and acceptance commands, or the TOML source. Forms show effective values;
info buttons provide contextual help with keyboard focus and Escape dismissal. The status page includes runtime defaults,
registered tasks, and on-demand queue exclusion reasons and `doctor` results.
Saving configuration and querying status do not execute tasks or start a worker.

**Add project** automatically maintains `~/.config/devrelay/projects.toml`, or `$XDG_CONFIG_HOME/devrelay/projects.toml` when `XDG_CONFIG_HOME` is absolute. An empty dashboard is supported; no hand-written catalog, project wrapper or dedicated runtime is needed.

Checking a repository is read-only. Confirmation preserves or creates configuration, adds missing Git ignores, registers the controller and saves the catalog. Changed configuration/catalog revisions require another check. Worktrees are deduplicated by Git common directory and use the existing controller's configuration. Source files, indexes, tasks and evidence are preserved.

Missing directories remain visible. Use **重新定位** (Relocate) to select the new path; repository identity is checked. A different repository, copied state still used elsewhere, active workers, workspace reservations and controller moves with saved tasks are rejected. Restore the original path when task history exists. Register a legacy hand-written entry at its original path before moving it so its identity is recorded. Relocation updates registration; it does not move files.

Agents and terminal users share this workflow:

```bash
devrelay projects add /path/to/project --name "My project"
devrelay projects list
devrelay projects locate PROJECT_ID /new/path/to/project
```

`projects add` is repeatable, preserves existing configuration and reuses an established controller. `init` remains available separately. Registration does not write to GitHub; use `devrelay --project /path/to/project init --sync-github` or explicitly initialize GitHub in the dashboard.

For a separate catalog, use `devrelay ui --catalog /path/to/projects.toml --open` or `devrelay projects --catalog /path/to/projects.toml add /path/to/project`. Paths resolve relative to the catalog. Writes use a lock, atomic replacement and exact-file backups under adjacent `catalog-backups/`. Retrying registration recovers partially completed local setup without resetting tasks.

Projects share the installed runtime by default. An advanced trusted catalog may set `runtime` to the directory containing a different `devrelay/` package; this loads Python code from that path. Configuration operations use that runtime's validation, defaults and locks without silently falling back. Registration preserves existing runtime overrides; it does not create per-project runtimes. The catalog neither upgrades the tool nor defines a `--workspace` task. See the [advanced example](examples/ui-projects.toml).

Preview validates changes and displays the TOML diff and affected evidence stages. Ordinary field edits preserve unrelated
content and scalar-line comments; editing a command list rewrites that list. Use the source editor for complex TOML layouts.
Saving rechecks file/runtime revisions, takes the repository lock, backs up the exact original under the Git common
directory's `devrelay/ui-backups/` using its content hash, and atomically replaces the configuration. The index, tasks and
historical evidence remain intact. The current session's backup path is available in the status page's project details; paste a backup into the source editor to preview
and restore it. Concurrent file changes require a reload instead of being overwritten.

The server binds only to `127.0.0.1` and protects writes with same-origin checks and a session token. Active workers,
surviving command processes and workspace reservations block saves. An available DevRelay lock does not establish that an
external agent has stopped editing. The next command loads the saved configuration; existing tasks still pass authorization
and delivery gates, and affected evidence may need regeneration. Keep credentials out of configuration and use existing
local `gh` / Codex logins.

## Autonomous projects

Agent discoveries created through DevRelay default to independent triage, implementation when in scope, and automatic PR review. Human Issues still need explicit approval by default; `queue.mode = "autonomous"` extends the same investigation pipeline to them. Automatic merging is a separate opt-in.

For a new project, use `devrelay init --provider github --mode autonomous` (discoveries during work are enabled by default in both modes). For an existing project, edit `.devrelay/config.toml`:

```toml
[queue]
mode = "autonomous"  # Default manual keeps human Issues subject to explicit approval.
auto_kinds = ["bug"]
scope = "Fix confirmed bugs with clear expected behavior; ask maintainers about product or architecture decisions."
max_dispatches = 20

[discovery]
enabled = true

[delivery]
auto_review = true
auto_merge = true
required_checks = ["test"]  # Use the project's actual required CI names.
```

Review the existing `[codex]`, `[[setup]]` and `[[checks]]` settings, then run:

```bash
devrelay doctor --with-codex
devrelay watch
devrelay status  # In another terminal: outcomes, decisions and remaining budget.
```

Flow: **Issue / discovery → deduplication and investigation → scope and acceptance → implementation → checks → independent review and bounded repair → CI → merge**. Investigations use separate detached checkouts of the target base; reproduction files never flow into implementation branches. Observations, logs and historical reports are retained. Duplicates or disproved reports become `skip` without closing their Issues. Missing information, unavailable reproduction resources and unresolved decisions become `needs_input` with concrete questions. Triage is an agent judgment; actual acceptance and independent review remain mandatory for automatic delivery.

Allowed `auto_kinds` are `bug`, `feature` and `maintenance`, defaulting to bugs; `scope` further restricts them. Tracking parents remain non-executable. An explicit `auto:approved` label authorizes an individual scope exception, without bypassing blockers, dependencies or acceptance. In autonomous mode, stop a task using a configured blocker such as `blocked` / `needs-input`; removing the approval label alone does not revoke project-level authorization.

Read questions in `status.next_action`, put decisions in the Issue **body**, and remove blockers when appropriate. `watch` re-investigates changed input and resumes started work in its original implementation session. Comments are not consumed yet. Known task-local handoffs, review decisions and exhausted repair budgets park that task while other independent tasks continue. Configuration, network/model faults, setup failures, unknown state, uncertain merges and failed post-merge acceptance still stop the worker. Pending CI and remote merge queues continue to wait serially.

The automatic queue defaults to 20 dispatches (only automatic discoveries count in manual projects; all tasks count in autonomous projects): an investigation or an entire implementation/delivery cycle each costs one, including failures; CI polling does not repeatedly charge. Usage is saved before launch and survives restarts. `--limit` still counts successful deliveries in the current invocation. After inspecting an exhausted budget, use `devrelay watch --reset-budget` to explicitly renew it. Per-task repair remains bounded by `codex.max_attempts`.

External agents can investigate through the same tools without invoking managed Codex:

```bash
devrelay next                         # Inspect triage_candidates.
devrelay triage 12 --prepare          # triage.context includes checkout and prompt/schema run directory.
# Investigate there; follow result-schema.json and echo context_sha256 exactly.
devrelay triage 12 --file result.json
devrelay start 12                     # After ready, continue prepare/check/submit.
```

`triage 12` starts managed investigation; `triage 12 --retry` explicitly re-investigates while retaining history. Results bind Issue content, project scope, setup and acceptance configuration; base changes during investigation reject results. Changed requirements/policy require a new decision. Automatic verification and review also bind the actual investigated scope. Existing projects retain `manual`; init preserves existing configuration and autonomous mode does not implicitly enable merging.

## Continue a task

When DevRelay should launch the implementation agent, use one entry point:

```bash
devrelay work 12          # Implement → check → review → bounded repair → deliver
devrelay status           # Read next_action, feedback and evidence locations
devrelay work 12          # Continue without replenishing the budget
devrelay work 12 --retry  # After resolving a handoff, explicitly open another budget
devrelay watch --limit 2  # Process two tasks through the same loop
```

Without an ID, `work` recovers pending work before selecting an authorized task. GitHub projects deliver a PR; local projects deliver a verified commit. `run --submit`, `resume ID --submit` and `watch` share this continuation path; `resume --submit` explicitly opens another budget. External agents remain primary callers of the engineering tools, which do not take over their sessions.

People define scope, acceptance and authorization. The original implementation session repairs failed checks, actionable AI review and explicit CI failures, rechecks and updates the same PR; new commits receive fresh independent review. Missing resources, incomplete review, required human approvals, unresolved threads, authorization problems and budget exhaustion preserve work and report `handoff` / `next_action`. Automatic repair cannot weaken acceptance or decide product scope.

`codex.max_attempts` limits total implementation calls in a continuation budget, including initial implementation and repairs (default: 2). Charge before launch; restarts and CI waits do not replenish it. History and evidence remain available. Base advances are merged automatically only into clean task branches, followed by checks, submission and review; edits or conflicts preserve the scene for recovery.

Project `[[checks]]` define behavioral QA: end-to-end tests, browser acceptance or project scripts. Source review does not prove unconfigured user flows. Reuse across projects requires their own setup and acceptance commands.

Evidence expires by stage: setup changes invalidate preparation, checks and review; check changes invalidate checks and review; model/profile changes invalidate review only. Ordinary queue/delivery-policy changes do not rerun setup or local checks; changed automatic scope or triage conclusions rebind acceptance. Configuration changes during an operation still stop it. Legacy receipts remain compatible only under their original complete configuration; otherwise regenerate affected evidence. Source, requirements and base identity remain strictly checked.

## Agent-discovered follow-ups

This fork records discoveries and leaves automatic triage and execution disabled for them. New issues must be authored by `vittta` and receive the `auto:approved` label before execution.

Implementation and independent review agents collect new problems encountered during their work by default. The defaults are:

```toml
[discovery]
enabled = true
max_per_task = 3
auto_triage = true  # Default: independently investigate before implementation.
auto_review = true  # Default: review discovery PRs; merging is separate.
```

Continue using `work/watch`. Agents return independently actionable, evidenced, out-of-scope bugs in `discoveries`. DevRelay retains the source task, observed HEAD/source tree, description, reproduction or code evidence, and acceptance criteria, then deduplicates and creates GitHub Issues. Local projects receive tasks awaiting triage. Reporting during existing agent turns defaults to enabled, including existing configurations that omit `discovery.enabled`. Set `enabled = false` to opt out; existing explicit opt-outs remain respected. Reporting adds no repository audit or separate model call; subsequent triage and PR review launch independent agents. The default cumulative limit is three distinct discoveries per source task across retries, configurable from 1 to 20.

New tasks receive no approval label. `discovery.auto_triage = true` and `discovery.auto_review = true` are defaults, including existing configurations omitting these fields. Even with `queue.mode = "manual"`, confirmed in-scope discoveries proceed after independent investigation and their PRs are reviewed automatically. `delivery.auto_review = false` does not disable discovery review; set `discovery.auto_review = false` to opt out of this source policy. Global review/merge settings still take precedence. Set `discovery.auto_triage = false` to opt out of automatic discovery authorization; manual projects then require the approval label or local `devrelay task approve ID`. Agent-reported evidence is not independent verification. Regressions introduced by the current change, failed acceptance and unfinished requirements remain with the original task and cannot be moved into follow-ups to bypass delivery gates.

External agents use the same intake without enabling managed discovery or installing Codex:

```bash
devrelay discover 12 --file /path/to/discoveries.json
devrelay status
devrelay discover 12  # Retry saved reports without another model call.
```

The file is a JSON array with four nonempty strings per report: `title`, `description`, `evidence`, and `acceptance`; see the [example](examples/discoveries.json). The source task must have a worktree for intake; publication rechecks its authorization. Models do not create or authorize Issues directly. The controller publishes them under the shared repository lock.

Deduplication uses persistent markers and titles normalized for case and whitespace, including closed Issues; it never reopens, overwrites or reauthorizes a match. Source policy requires a retained controller publication receipt and the matching Issue marker. A copied marker, label or bot author alone cannot grant it. Preserve the generated marker and local runtime state; migration without those records requires explicit authorization. Differently worded reports of the same bug can still need manual reconciliation. Reports and repeated observations remain in the source task's `discoveries`; publication failures appear in `discovery_error` without changing the original acceptance result. After an uncertain GitHub creation request, ordinary retries only read back. Use `devrelay discover 12 --retry-create` only after confirming no remote Issue was created. A publication failure does not block delivery of the original task; recover it separately using `discovery_error.next`.

## Multi-repository autodev MVP

One authorized outcome can coordinate multiple repositories through an explicit
workspace manifest, run member and joint acceptance, and deliver linked draft PRs.
`run/resume` uses one implementation session and bounded check/independent-review
repair loops. This MVP delivers drafts only, with no automatic merge. See
[workspace operation and recovery](docs/workspace.md) and the
[configuration example](examples/workspace.toml). Existing `--project` behavior remains.
Each acceptance round retains separate, digest-bound logs/artifacts; once independent
review starts, repairs require fresh approval. Partial publication uses remote commit
checkpoints. For delivered tasks, `submit` only retries reservation release.
Confirmed pushes resolve their recovery intent; external rewinds block publication.
Member and joint checks retain separate logs even when their IDs match.

```bash
devrelay --workspace /path/workspace.toml task add terrain-edit --title "Local terrain edit" --file /path/task.md
devrelay --workspace /path/workspace.toml run terrain-edit --submit
```


## Install and onboard

Install one user-level command. With `uv` installed:

```bash
uv tool install git+https://github.com/vittta/DevRelay.git
devrelay --version
devrelay ui --open
```

If `devrelay` is not found, run `uv tool update-shell` and reopen the terminal. All projects use this command; no source checkout, project-specific virtual environment or wrapper is required. `pipx install git+https://github.com/vittta/DevRelay.git` is an alternative.

Update with `uv tool upgrade devrelay`. To pin or restore a reviewed commit, replace `REVISION` with its SHA:

```bash
uv tool install --force 'git+https://github.com/vittta/DevRelay.git@REVISION'
```

Installation and updates do not migrate or delete catalogs or task state. `doctor` reports the actual package path, Python executable and commit ID for Git installs; the dashboard also exposes runtime paths. Preserve the catalog before downgrading: old dashboards without `repository_id` support need a matching catalog from `catalog-backups/`, or `--project` to open a single project. Do not delete repository state to switch versions.

Editable source installation is for DevRelay development:

```bash
cd /path/to/DevRelay
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/devrelay --help
```

Engineering tools do not require Codex CLI; optional orchestration and AI review require an installed, signed-in Codex CLI. GitHub tasks and PR submission require authenticated `gh`. Existing authentication is reused; credential files are not copied.

For terminal onboarding:

```bash
devrelay projects add /path/to/project
# Review /path/to/project/.devrelay/config.toml
devrelay --project /path/to/project doctor
```

`init` detects GitHub origin, the default branch, Node package managers and common scripts, Python installation, Cargo and Go checks. It creates TOML configuration and appends runtime ignores (including Python environments and installation artifacts) without overwriting existing configuration or AGENTS.md. It does not install packages or call a model. Review watch modes, service requirements and actual business acceptance. Python test frameworks are not guessed; configure the real runner.

Commands use argument arrays, with no shell expansion. Setup runs in the task worktree. At least one acceptance command is required before run/check/submit. Commit the project configuration, ignore rules and generated issue templates. Empty repositories need an initial commit. Codex uses the user's default model unless `codex.model` or `codex.profile` is set. See [configuration](examples/config.toml).

```toml
[project]
provider = "github"
repository = "owner/project"
base_branch = "main"

[codex]
# Optional orchestration and AI review; omit this table for engineering tools alone.
sandbox = "workspace-write"
max_attempts = 2

[[checks]]
id = "test"
argv = ["python3", "-m", "unittest", "discover", "-s", "tests"]
```

## Primary workflow: agent-driven

The external agent investigates goals, explores alternatives, defines independently verifiable tasks, implements and adjusts its plan from results. Explicitly authorized child tasks enter the existing queue; DevRelay provides engineering operations and delivery gates. Plans and experimental conclusions can live in Issues, task files and project artifacts.

```bash
devrelay --project /path/to/project next
devrelay --project /path/to/project start 12
devrelay --project /path/to/project prepare 12
# The agent investigates, implements and repairs in the returned worktree.
devrelay --project /path/to/project check 12
devrelay --project /path/to/project submit 12
```

Call tools from the controller checkout and read worktree, state and evidence paths from JSON results. `start` only claims and creates or recovers the worktree. `prepare` runs all configured setup commands in an already claimed worktree without invoking a model. Each explicit call reruns setup and invalidates previous acceptance, AI review and merge-gate records while preserving historical logs; run `check` afterwards. Failed or interrupted preparation preserves work and logs; fix the cause and retry `prepare`. When setup is configured, missing, failed or stale preparation blocks acceptance and delivery. `check` never starts an implementation agent; the external agent inspects failures, repairs and checks again. `submit` defaults to a draft PR and advances the automatic review/merge policy applicable to that task; discovery PRs are reviewed by default.

Tasks with a local commit or an unmerged PR can run `prepare`, then `check/submit`, retaining their original branch and PR. Existing PRs are read back to verify repository, state, head and base. Closed, merged, actively merging or mismatched tasks reject preparation and require delivery recovery instead. Integrate the latest target base into the task branch after environment configuration changes. Submission rechecks title, body, authorization, source, configuration and base at commit, push, PR publication and readback boundaries. Observed drift stops progression while preserving commits, pushes and any read-back PR identity; revalidate to recover, without automatic rollback.

`submit` reads the saved PR URL before committing or pushing and verifies its repository, open state, head, base and pending merge request. Closed or changed PRs block further publication. After uncertain creation, it searches every PR state for the branch and retains the original PR without creating a replacement. Task records preserve the published HEAD, push intent and latest PR observation; retries after an uncertain push accept only the recorded old or new HEAD before continuing with the same PR.

The external host owns agent sessions, plans, experiment budgets and exclusive editing between tool calls. DevRelay's worker lock covers tool execution, not editing between calls; do not start `watch` for that repository at the same time. Recover by inspecting `status` and the actual worktree, then continue editing or retry the tool. `resume` is specific to optional DevRelay Codex orchestration and cannot resume an external agent's session.

`doctor` reports missing target bases or invalid fetch mappings as failed checks and continues the remaining diagnostics. It checks engineering tools and AI delivery dependencies, including default discovery PR review for GitHub projects. External-only workflows without built-in review can disable `discovery.auto_review` and global automatic delivery. Run `doctor --with-codex` before optional orchestration or explicit `review`. See the [agent workflow](docs/agent-workflow.md) for the complete operating agreement.

The GitHub login check uses `gh api --hostname github.com user --silent` to verify API access with the effective credentials, including credentials supplied through environment variables. Expired credentials for inactive accounts do not cause false failures. Authentication failures, network/API errors, an unavailable command or a timeout still produce `github_login=false` and `ready=false`. This check does not establish read/write permissions for the target repository; DevRelay does not switch accounts or change authentication configuration.

## GitHub queue

### Labels and issue templates

For GitHub projects, first-time `init` uses authenticated `gh` to create missing repository labels and generates `.github/ISSUE_TEMPLATE/devrelay-{bug,task,experiment,plan}.md`. Label creation requires the corresponding repository permission. Local onboarding remains offline. Labels are created on GitHub immediately; templates are local files and appear in GitHub's issue chooser after being committed to the repository default branch. Initialization does not commit or push files.

Defaults include generic task and queue labels; projects own their domain labels:

| Dimension | Labels |
|---|---|
| Primary work type | `bug`, `enhancement`, `documentation`, `experiment`, `infrastructure` |
| Overall plan | `scope:plan` (tracks children; rejected for execution) |
| Optional priority (at most one) | `priority:P0` (urgent fixes), `priority:P1` (core delivery), `priority:P2` (extensions) |
| Authorization | `auto:approved` |
| Blockers | `blocked`, `needs-input` |

One issue delivers one observable, reviewable result. Keep the code, configuration, tests and documentation necessary for that result together. Split child issues only when they have independent value or acceptance criteria, not merely by file, layer or phase. Maintainers confirm this scope before authorization; implementation and AI review prompts carry the same rule. The queue does not automatically judge whether a natural-language task should be split.

Templates record evidence, scope, acceptance commands, one `Depends on: none` declaration, resources and recovery conditions. Bug, task and experiment templates pre-apply only the work type. The plan template pre-applies `scope:plan` and includes `Task kind: plan` in its body to track child deliveries. **Generated templates never automatically apply `auto:approved`, a configured authorization label, or a priority to new issues.** Creating a repository label does not authorize an issue. Manual-mode maintainers grant execution authorization; autonomous projects apply their investigation policy. Priority remains optional; dependencies and blockers still apply.

Tracking parents carrying `scope:plan` or a standalone body line `Task kind: plan` are rejected even if accidentally authorized. The original template's `## Child issues and delivery plan / 子任务与交付计划` heading is also recognized. This applies to GitHub and local tasks, automatic selection and authorization for explicit-ID execution, resume and submission. A child's `Parent: #N` link does not affect eligibility; do not declare its tracking parent as a prerequisite that must be completed first. Maintainers must mark custom plans without these indicators; `init --sync-github` preserves existing templates rather than upgrading them.

Existing label names, colors and descriptions, template files and other forms are preserved. Preserved project templates retain their own defaults and should be reviewed separately. Add project-specific area labels as needed. For already-onboarded projects or recovery after network/permission errors:

```bash
devrelay --project /path/to/project init --sync-github
```

This uses existing configuration without overwriting configuration, ignore rules or templates, adding only missing items. Custom `[queue]` authorization, priority and blocker labels are honored and described in generated templates; a type label reused as a queue control is not pre-applied. Uncertain creation responses are read back before continuing. Only complete template files are published, so interrupted writes can recover missing files. Required archived labels or queue-label spelling mismatches require a maintainer to restore the label or align configuration.

### Selection and delivery

Tasks must be open, authorized under project policy, and have no `blocked` / `needs-input`. Manual mode requires `auto:approved`; autonomous mode also accepts a current in-scope ready diagnosis. Priority is optional (default: the second configured priority, or the first if only one exists); multiple priority labels are rejected. Omitted `Depends on` means no dependencies; explicit declarations must be valid and unique. Dependencies must be closed and linked to merged PRs whose merge commits are ancestors of the refreshed target branch. Closing issue references must match the repository and number in their full GitHub URLs; same-number issues in other repositories neither satisfy dependencies nor claim local work through open PRs. Existing worktrees and explicitly associated open PRs prevent duplicate claims.

```bash
devrelay next
devrelay start 12
devrelay prepare 12
devrelay status
devrelay check 12
devrelay submit 12
```

`next` explains selection and exclusions without claiming work; it reads GitHub and fetches the base. Beyond the CLI's 1,000-item window, cursor pagination reads the full inventory rather than rejecting a repository for its lifetime totals. Incomplete pages still block; full snapshots of large repositories may be slow. Engineering tools and orchestration share authorization, dependency and acceptance rules. An agent report does not replace current evidence produced by `check`.

`submit` requires current verification evidence, commits, pushes and creates or updates a **draft PR**. `run --submit` combines the steps. A GitHub remote must resolve to exactly one fetch URL and one push URL, both targeting `project.repository`; SSH and HTTPS may be mixed. Multiple fetch/push URLs are rejected instead of allowing Git push fan-out. To bind the actual endpoint, `insteadOf` / `pushInsteadOf` URL rewrites affecting that remote are also rejected explicitly; configure the GitHub URLs directly. Git configuration is validated from the worktree that performs each transport, so branch- or path-conditional `includeIf` configuration cannot bypass the check. Submission and cleanup use an absolute-path transport argument that cannot select a named remote, bound to the validated URL in the same Git invocation. Neither a rewrite nor a URL-named remote alias added after validation can redirect or fan out the transport; existing credential and CA settings remain available. Submission records the configured remote/merge tracking settings directly even for single-branch clones, and updates the tracking reference selected by the existing fetch refspec rather than assuming `refs/remotes/<remote>/<branch>`. Cleanup uses the same mapping. By default delivery retains draft status; discovery PRs also receive independent review. Configure global review and merge below. PRs use `Refs #N`, preserving issue scope. DevRelay does not close whole issues, release, deploy or create background schedules.

When a remote targets a different repository, the error identifies the fetch or push URL with `does not match project.repository`; correct that remote before retrying GitHub operations.

The target base, tracking updates after push and tracking cleanup use the first matching positive fetch refspec. The target branch must have a positive fetch mapping; a missing mapping stops the operation and requires configuration repair instead of falling back to stale refs such as `origin/main`. Negative fetch refspecs restrict fetching, but do not cancel this tracking mapping.

Transport preserves Git's `GIT_CONFIG_NOSYSTEM` boolean policy: `0`/`false` keeps the original system settings, while `1`/`true` disables them. Other Git boolean spellings are supported; invalid values stop the transport.

## Optional mode: Codex orchestration

Explicitly invoke these commands when DevRelay should launch the implementation agent:

```bash
devrelay doctor --with-codex
devrelay run 12             # Setup → Codex implementation → acceptance
devrelay run 12 --submit    # Then deliver under the project policy
devrelay resume 12          # Resume the saved DevRelay implementation session
```

`run` handles one task, recovering interrupted work first when no ID is given; failed or blocked work returns `needs_attention` until explicitly addressed. Codex implements in `.devrelay/worktrees/ID`; DevRelay runs configured checks. Failed checks feed back into the same saved Codex session within `max_attempts`. External blockers are retained. All execution entry points exit 1 for failed, blocked, rejected-review or `needs_attention` results. An empty `idle` queue, delivered PR and `merge_waiting` without blockers exit 0; a waiting merge with explicit blockers exits 1.

Do not let no-ID `run` or `watch` take over tasks still being edited by an external agent. Stop the previous executor and inspect the worktree before switching modes. Both paths use the same authorization, dependency, verification and delivery gates; an agent report does not replace current verification evidence.

### Continuous issue dispatch

Run from an onboarded GitHub project:

```bash
devrelay --project /path/to/project watch
# Change the empty-queue interval, or stop after delivering two PRs:
devrelay --project /path/to/project watch --poll-interval 30 --max-tasks 2
```

`--interval` is a shorter alias for `--poll-interval`, for example `watch --interval 30`; both accept positive integers and default to 60 seconds.

`--limit` is a shorter alias for `--max-tasks`, for example `watch --limit 2`; both accept positive integers, with no delivery-count limit when omitted; autonomous projects still have a persistent dispatch budget.

`watch` serially claims authorized issues with integrated dependencies, ordered by configured priority and creation time. It implements, verifies, submits a draft PR and runs enabled independent read-only review. `[delivery] auto_merge` in the project's `.devrelay/config.toml` explicitly selects the delivery mode; it defaults to `false`, and the CLI does not enable authorization:

- **Manual merge** (`auto_merge = false`): successful draft submission and enabled review allow the next issue immediately. Drafts remain drafts, and submitted tasks are not implemented again.
- **Automatic merge after acceptance** (`auto_merge = true`): independent review is mandatory. After evidence, CI, GitHub reviews and branch protection pass, request an exact-head merge, wait for GitHub's `MERGED` readback, perform configured post-merge acceptance and cleanup. Normally `completed` tasks allow the next claim; autonomous projects can also park known task-local handoffs before continuing independent work. Pending CI, required human approvals and protection conditions are polled at `--poll-interval`; no other issue is claimed during the wait.

An empty queue is also checked at `--poll-interval` (default 60 seconds), so newly authorized issues can be picked up. Existing authorization, blockers, dependencies, duplicate-claim and acceptance gates still apply. See the configuration example below; editing configuration alone does not start an executor.

The command runs in the foreground; keep the terminal and machine running. It does not install a background service or startup job. Output is flushed JSON Lines events followed by a final status: `watch_started.mode` is `manual_merge` or `auto_merge`, and `merge_waiting` events explain waits. `--max-tasks` counts deliveries completed during this invocation, including recovered deliveries: successful PRs in manual mode, tasks passing post-merge acceptance and cleanup in automatic mode. Omit it to keep watching.

The repository worker lock remains held during empty-queue, CI and merge-queue waits. `status` / `next` remain available; stop `watch` before other DevRelay mutations. `Ctrl+C` or `SIGTERM` retains work and exits 130. Restart prioritizes interrupted implementation, submission and delivery. Automatic mode reads back existing PRs, reuses current review evidence and recovers queued merges, post-merge acceptance and cleanup. A successful review process with complete saved evidence recovers its result directly; incomplete review evidence becomes `review_blocked`, requiring explicit `review ID` without silently repeating review. Manual-mode PRs remain for maintainer integration.

Failed checks, actionable independent review and explicit CI failures enter the bounded repair loop. For manually authorized tasks, external blockers, incomplete review, unknown CI states, unresolved GitHub feedback, conflicts, withdrawn authorization, network/API errors and budget exhaustion preserve evidence and stop dispatch. Automatic discoveries and autonomous projects park known task-local handoffs and exhausted repair budgets; unknown state and repository failures still stop dispatch. Pending CI and protection conditions keep waiting. Read `status.next_action`, resolve the cause and use `work ID --retry`. An uncertain merge request still requires the dedicated `merge ID --retry-request` recovery.

## Automatic review and merge

Configure and commit this policy before running tasks:

```toml
[delivery]
auto_review = true
auto_merge = true
required_checks = ["test", "build"] # Actual GitHub check names
allow_no_ci = false
cleanup_worktrees = true
cleanup_branches = true
```

Both global `[delivery]` switches default to `false`; discovery PRs separately default to `discovery.auto_review = true`. Review alone retains draft status. Automatic merge includes review, marks an approved draft ready and requests squash merging when gates pass. Named required checks prevent missing or not-yet-created CI from being ignored; required checks must succeed; nonrequired SKIPPED checks are allowed. Neutral, failing and pending checks do not pass. No CI evidence blocks by default. Explicitly set `allow_no_ci = true` only for projects without GitHub CI; local acceptance, AI review and any named required checks still apply.

```bash
devrelay submit 12         # Verified implementation → draft → enabled review/merge policy
devrelay review 12         # Explicit fresh, read-only AI review
devrelay merge 12          # Requires auto_merge; recheck gates or recover a merge
devrelay resume 12 --submit
```

Review uses a fresh Codex session with the `read-only` sandbox, preserving the implementation session. Reports remain local; AI review does not submit a GitHub APPROVE or replace required human approvals. Negative/incomplete review, source/head/base/configuration/issue drift, withdrawn authorization, unresolved threads and GitHub merge restrictions block merging. API errors or truncated inventories fail closed; up to 100 checks, reviews and threads are supported per snapshot.

Merge uses `--squash --match-head-commit SHA`, with no admin bypass. A successful request is not completion: DevRelay reads back the merged state and commit. CI waits and merge queues return `merge_waiting`; rerun `merge` or `run --submit` to advance. A single `merge` invocation is bounded; `watch` with `auto_merge` polls until post-merge acceptance completes. GitHub merge queues determine the actual merge strategy; DevRelay does not bypass the queue.

Before issuing the merge command, DevRelay persists `merge_request` with the head, base, source, configuration and task evidence. Queued requests are only read back, without repeated mutations; timeout/crash recovery also reads first. If the PR is still unmerged and neither a queue entry nor remote auto-merge exists, delivery blocks. Inspect GitHub, then explicitly use `devrelay merge ID --retry-request` to retry. This still requires current acceptance, independent review, authorization and all gates; an existing queued request only waits.

Revoked authorization, changed configuration/evidence or blocking feedback on a queued request triggers an attempt to cancel the recorded remote auto-merge/queue entry and verify readback, saving `merge_cancellation`. Cancellation failures preserve a clear error and require removing the pending request on GitHub before recovery. `Ctrl+C`/`SIGTERM` retain queued requests for restart readback; GitHub may continue processing while the controller is stopped. Authorization checks, cancellation and remote merging can race: an already merged PR is recorded and stops for inspection; a completed remote merge cannot be cancelled.

Crash recovery reads only the task’s direct, canonical review run and non-symlink evidence files. The successful process receipt must match its worktree, log and configured read-only review command. Missing, mismatched or out-of-scope evidence blocks recovery and requires explicit `review ID`; unrelated approvals are not reused and approval output stays in the owned run.

After merge, DevRelay always verifies base ancestry, merge identity and exact tree equality. By default it also runs setup/checks in a fresh detached worktree. Set `delivery.post_merge_checks = false` to omit those commands and the temporary worktree; identity/tree checks still apply. Receipts record `commands_run`; re-enabling the option runs the omitted checks. Successful configured acceptance records `completed`. API timeout recovery reads GitHub first and never re-merges an already merged PR. Unexpected trees or failed post-merge checks record `post_merge_failed`, retain evidence and failed worktrees, and block automatic dispatch without an ID. GitHub's head condition does not atomically lock the base, so pre-merge refresh and post-merge tree verification both matter. A post-merge failure needs maintainer remediation; DevRelay does not automatically revert a completed remote merge.

To revise a PR, fix its preserved worktree and run `check ID`, then `submit ID`. New commits invalidate old reviews. Configuration changes invalidate only affected evidence stages, as described above. Maintainers still own issue closure and dependency delivery links.

### Post-merge cleanup

`cleanup_worktrees` and `cleanup_branches` default to `false`; opt in before running tasks. Worktree cleanup removes the successful task's implementation checkout. Branch cleanup removes its local, remote and remote-tracking branches and requires worktree cleanup too. Enable only worktree cleanup to retain branches.

Cleanup requires a merged PR, its merge commit in the refreshed base, and successful configured post-merge acceptance. Failed acceptance retains the task branches, implementation checkout and failed acceptance worktree. Even after a successful retry, previous failed worktrees remain listed in `retained_worktrees`. Successful temporary detached acceptance worktrees are reclaimed as before.

Only recorded task paths and exact commits qualify. Worktrees are never force-removed or broadly pruned. New tracked/non-ignored files, locks, advanced branches and other worktrees using the branch stop cleanup. Removing a worktree also removes its ignored environments, caches and outputs; keep durable files outside it. Verification, review, cleanup logs and shared task state remain in the Git common directory.

Cleanup errors record `cleanup_failed` separately from successful acceptance and pause automatic dispatch without an ID. Some earlier steps may already have removed resources. Resolve permissions, locks or new changes, then run `devrelay merge ID` to inspect actual Git state and resume cleanup without re-merging or repeating still-valid acceptance. A branch already deleted by GitHub is handled idempotently. Remote deletion and local ref deletion match the expected SHA; changed branches are retained. Disable GitHub's own automatic branch deletion if DevRelay must control deletion timing: it cannot delay externally triggered deletion.

## Local tasks

```bash
devrelay init --provider local
devrelay task add fix-1 --title "Fix a bounded issue" --file /path/to/task.md
devrelay start fix-1
devrelay prepare fix-1
# The external agent implements in the returned worktree.
devrelay check fix-1
devrelay submit fix-1 --local
```

Adding a local task explicitly approves it; a missing dependency declaration becomes `Depends on: none`. Local dependencies may use `Depends on: #fix-1`. Local submission creates only a verified local commit. Integrate it into the configured base, then call `devrelay task complete fix-1` before dependent tasks can run. Completion requires current verification matching the requirements and configuration, a worktree and HEAD matching the delivered commit and verified tree, and that commit in the target base ancestry. Newly verified content must also be submitted and integrated; failed completion does not unlock dependencies. See the [task template](examples/task.md).

## Evidence and recovery

Verification binds source tree, HEAD, target base, configuration and task requirements. Temporary Git indexes hash tracked changes and non-ignored new files without modifying the original index. Checked-out submodules are inspected recursively for tracked changes and non-ignored untracked files. Isolated-index comparisons neutralize `assume-unchanged`, `skip-worktree`, `core.fileMode=false`, `submodule.*.ignore`, `.gitmodules` ignore settings and status configuration that could hide those changes. Any dirty submodule blocks acceptance, submission and automatic review/merge gates. DevRelay never commits, cleans, resets or updates submodule work.

Acceptance in the main repository requires raw bytes of included regular files, symlink types and targets to match the Git tree that will be delivered. The isolated index still uses Git’s normal conversion rules. Filters that preserve the current bytes remain supported. Byte-changing clean/smudge filters, CRLF normalization or `working-tree-encoding` conversion stop acceptance explicitly, preserving the worktree and original index. Use a checkout whose bytes match the deliverable blobs before verification. DevRelay does not rewrite files or bypass legitimate transformations to commit different content.

All Git operations that generate source evidence disable local replacement refs and inspect actual commit objects, leaving replacement refs untouched. NUL-delimited paths are parsed as bytes, supporting non-UTF-8 filenames and carriage returns where the filesystem permits them.

Untracked submodule files are detected using only `.gitignore` rules, without local `info/exclude` or `core.excludesFile` rules. Detection and isolated-index comparisons force `core.ignoreCase=false`, so a rule such as `build/` cannot hide `BuIlD/undelivered.txt`; user configuration is left unchanged. Staged content is compared using a read-only index operation; source evidence does not rebuild cache trees in real submodule indexes and disables optional index refresh writes.

Tracked regular files in submodules also have their raw bytes compared against HEAD blobs, bypassing attributes and clean filters so local transformations cannot hide undelivered content. Submodule worktree bytes must match the commit; checkouts that require line-ending normalization, encoding conversion or smudge filters to match committed blobs block acceptance.

Committed symlinks in submodules must remain symlinks, and their raw `readlink` target bytes must match HEAD blobs. Acceptance blocks when `core.symlinks=false` materializes a link as a regular file or hides such a replacement; verify in a worktree that supports symlinks.

Clean committed submodule HEAD changes are included in the source tree through the parent gitlink and can be delivered normally; submission explicitly stages checked-out gitlinks independently of submodule ignore configuration. An uninitialized submodule path must be absent or empty; residual files block acceptance. Projects whose checks need submodule contents should initialize them explicitly in `[[setup]]`. Source/history/base/configuration/requirements drift invalidates evidence. Integrate a newer base manually and rerun checks; existing work is never reset automatically.

SQLite state, a repository-wide worker lock, command logs, resource receipts, verification and Codex JSON events live under `devrelay/` in the Git common directory. Every attempt gets a new directory. DevRelay records that directory and event-log path before launching Codex, then persists the task's session ID as soon as it receives a valid `thread.started`. Before invoking Codex, `devrelay resume ID` validates the saved ID's format and cross-task ownership as well as the previous log, then resumes that exact session, never `--last`. An empty pre-launch log is safe to retry; incomplete, invalid or conflicting session events block recovery.

All CLI commands handle Ctrl+C and SIGTERM consistently, stop the active command process group and exit 130. Interrupted preparation, implementation and acceptance save `interrupted` status, work and logs; interrupted acceptance retains no valid passing evidence. Controller crashes cannot silently switch recovery to a new session. On timeout or interruption, DevRelay sends SIGTERM to the entire command process group, then sends SIGKILL if non-zombie members survive the bounded grace period and waits a bounded time for exit. It clears `active_process` only after the group is gone; a group that cannot be stopped retains the marker and blocks another worker. Every command launch, including automatic retries within the same worker, checks the previous marker. If that group is alive or a bounded inspection cannot confirm its exit, the launch is refused and the original marker is preserved.

Session ownership includes saved IDs, retained implementation events (including uncheckpointed IDs after a controller crash), and review receipts/events under each task's run directory, even after acceptance or review is invalidated. Implementation recovery rejects other tasks' sessions and review sessions, including its own; unsafe or unreadable evidence blocks recovery. Reads remain within owned run directories except for the strictly validated legacy receipt described below.

Completed legacy tasks may also reference `devrelay/integration/<32 lowercase hex characters>/review.json` in the Git common directory. This compatibility reader accepts only a non-symlink receipt that exactly matches the saved review and the completed task's PR URL and HEAD. It reserves the review session without changing the task or original evidence. Active tasks, mismatched or unreadable receipts, unknown layouts and symlinks still block recovery; arbitrary paths are never read.

Ownership harvesting retains every known session reservation without validating other tasks' entire turns. A clearly identified truncated non-session message does not block unrelated recovery; damaged or ambiguous session events still block it. The recovering task's own log remains fully validated, so its incomplete or conflicting events cannot be bypassed.

Codex JSONL events are separated only at LF boundaries, preserving Unicode separators inside messages even with chunked output. Stderr diagnostics are retained separately and are not parsed as JSON events.

New implementation runs record `events.jsonl.format.json` before launch and retain strict stdout JSONL parsing during recovery. Older unmarked logs without a separate stderr log may contain ordinary stderr diagnostics; recovery ignores that text but still rejects malformed JSON/session events and conflicting IDs. A diagnostics-only legacy log without a saved session blocks recovery rather than silently starting a new session.

Acceptance commands are trusted project code. A zero exit code does not establish business correctness or independent review. Projects control scoring rules, official engine requirements and delivery authorization.

The implementation session runs in a development sandbox; the controller runs all configured acceptance commands in its host environment. When implementation is ready but sandbox permissions prevent validation (such as `ps`, temporary-directory or filename access), the implementation policy directs Codex to report `ready` with the actual diagnostics in its summary. This does not claim acceptance or skip any check: host checks must still pass before submission. Genuine code defects must be fixed, and missing resources that prevent implementation remain blockers. The controller does not reinterpret or override a `blocked` result.

Source, HEAD and recursive submodule cleanliness are checked after each acceptance command, before the next command can run, as well as at finalization. Post-merge acceptance applies the same checks after setup and between acceptance commands. A dirty submodule fails verification immediately, retaining its changes and command evidence; a later command cannot restore it to hide the mutation. These are command-boundary snapshots, not continuous monitoring of changes restored within a single command.

## Develop

This repository's `.devrelay/config.toml` enables self-maintenance: approved GitHub Issues are implemented in isolated worktrees, verified with the full test suite, submitted as draft PRs, and reviewed in fresh read-only AI sessions. Automatic merge is disabled; run `devrelay merge ID` after merging to perform post-merge acceptance and cleanup.

The optional self-maintenance executor and AI reviewer are pinned to `gpt-6.1-sol`, verified with Codex CLI `0.162.1`. Upgrade an older CLI if it rejects this model. Changing the model configuration requires fresh review; still-valid setup and local acceptance are reused. The external agent's model is controlled by its host.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Tests use real temporary Git repositories and a fake Codex CLI. Delivery tests push and squash-merge in temporary repositories, run fresh post-merge checks, and mock GitHub review/CI APIs and timeout recovery. They do not call models or push public repositories. Real target projects still require their own acceptance. See [architecture](docs/architecture.md) and [official Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive).

Non-UTF-8 filename end-to-end cases run on supporting filesystems. Only fixture creation that explicitly rejects an invalid UTF-8 name with `EILSEQ` reports a platform skip, which is not successful product acceptance. Git tree/index byte-path regressions run without checking out these names on every platform; permission and product errors remain test failures.

## Installed-flow validation

CI runs the full suite on macOS/Linux and Python 3.11/3.14, builds a wheel, and exercises the installed CLI in a fresh virtual environment with an isolated user catalog outside the source checkout. It covers first registration, a fresh clone with existing configuration, configuration saves, duplicates, relocation, wrong-repository rejection, dirty files/index preservation and state retention after reinstallation. Only temporary local Git repositories are used; no remote tasks or models are invoked.

```bash
python3 -m pip wheel --no-deps . --wheel-dir dist
python3 scripts/smoke_install.py dist/devrelay-*.whl
```
