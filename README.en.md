# DevRelay

English | [简体中文](README.md)

General engineering tools for agents: agents plan, explore and implement, while DevRelay provides isolated worktrees, project acceptance and gated delivery.

The primary workflow is driven by an external agent calling `next/start/prepare/check/submit`; DevRelay records engineering state and verification evidence. `run/resume/watch` remain optional Codex orchestration. Projects own setup commands and acceptance rules. Version 0.4 supports macOS / Linux, Python 3.11+ and Git, with no third-party runtime dependencies.

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

```bash
cd /path/to/DevRelay
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/devrelay --help
```

Use `devrelay --version` or `devrelay -V` to print the version; neither a Git repository nor project configuration is required.

Alternatively, use `pipx install /path/to/DevRelay` for a global command. Engineering tools do not require Codex CLI; optional orchestration and AI review require an installed, signed-in Codex CLI. GitHub tasks and PR submission require authenticated `gh`. Existing authentication is reused; credential files are not read or copied.

```bash
devrelay --project /path/to/project init
# Review /path/to/project/.devrelay/config.toml
devrelay --project /path/to/project doctor
```

Initialization detects GitHub origin, the default branch, Node package managers and common scripts, Python installation, Cargo and Go checks. It creates TOML configuration and appends runtime ignores (including Python environments and installation artifacts) without overwriting existing configuration or AGENTS.md. It does not install packages or call a model. Review watch modes, service requirements and actual business acceptance. Python test frameworks are not guessed; configure the real runner.

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

Call tools from the controller checkout and read worktree, state and evidence paths from JSON results. `start` only claims and creates or recovers the worktree. `prepare` runs all configured setup commands in an already claimed worktree without invoking a model. Each explicit call reruns setup and invalidates previous acceptance, AI review and merge-gate records while preserving historical logs; run `check` afterwards. Failed or interrupted preparation preserves work and logs; fix the cause and retry `prepare`. When setup is configured, missing, failed or stale preparation blocks acceptance and delivery. `check` never starts an implementation agent; the external agent inspects failures, repairs and checks again. `submit` defaults to a draft PR, but still advances explicitly configured automatic review/merge policy.

Tasks with a local commit or an unmerged PR can run `prepare`, then `check/submit`, retaining their original branch and PR. Existing PRs are read back to verify repository, state, head and base. Closed, merged, actively merging or mismatched tasks reject preparation and require delivery recovery instead. Integrate the latest target base into the task branch after environment configuration changes. Submission rechecks title, body, authorization, source, configuration and base at commit, push, PR publication and readback boundaries. Observed drift stops progression while preserving commits, pushes and any read-back PR identity; revalidate to recover, without automatic rollback.

`submit` reads the saved PR URL before committing or pushing and verifies its repository, open state, head, base and pending merge request. Closed or changed PRs block further publication. After uncertain creation, it searches every PR state for the branch and retains the original PR without creating a replacement. Task records preserve the published HEAD, push intent and latest PR observation; retries after an uncertain push accept only the recorded old or new HEAD before continuing with the same PR.

The external host owns agent sessions, plans, experiment budgets and exclusive editing between tool calls. DevRelay's worker lock covers tool execution, not editing between calls; do not start `watch` for that repository at the same time. Recover by inspecting `status` and the actual worktree, then continue editing or retry the tool. `resume` is specific to optional DevRelay Codex orchestration and cannot resume an external agent's session.

`doctor` checks engineering tools and configured AI delivery dependencies by default; enabling `auto_review` or `auto_merge` still requires Codex. Run `doctor --with-codex` before optional orchestration or explicit `review`. See the [agent workflow](docs/agent-workflow.md) for the complete operating agreement.

## GitHub queue

### Labels and issue templates

For GitHub projects, first-time `init` uses authenticated `gh` to create missing repository labels and generates `.github/ISSUE_TEMPLATE/devrelay-{bug,task,experiment,plan}.md`. Label creation requires the corresponding repository permission. Local onboarding remains offline. Labels are created on GitHub immediately; templates are local files and appear in GitHub's issue chooser after being committed to the repository default branch. Initialization does not commit or push files.

Defaults follow DexLab's management rules:

| Dimension | Labels |
|---|---|
| Primary work type | `bug`, `enhancement`, `documentation`, `experiment`, `infrastructure` |
| Overall plan | `scope:plan` (tracks children; rejected for execution) |
| Relevant area | `area:backend`, `area:benchmark`, `area:cloth`, `area:contact`, `area:evaluation`, `area:hardware` |
| Exactly one priority | `priority:P0` (urgent fixes), `priority:P1` (core delivery), `priority:P2` (extensions) |
| Authorization | `auto:approved` |
| Blockers | `blocked`, `needs-input` |

One issue delivers one observable, reviewable result. Keep the code, configuration, tests and documentation necessary for that result together. Split child issues only when they have independent value or acceptance criteria, not merely by file, layer or phase. Maintainers confirm this scope before authorization; implementation and AI review prompts carry the same rule. The queue does not automatically judge whether a natural-language task should be split.

Templates record evidence, scope, acceptance commands, one `Depends on: none` declaration, resources and recovery conditions. Bug, task and experiment templates pre-apply only the work type. The plan template pre-applies `scope:plan` and includes `Task kind: plan` in its body to track child deliveries. **Generated templates never automatically apply `auto:approved`, a configured authorization label, or a priority to new issues.** Creating a repository label does not authorize an issue. Maintainers separately select one priority and grant execution authorization after confirming the scope and acceptance; dependencies and blockers still apply.

Tracking parents carrying `scope:plan` or a standalone body line `Task kind: plan` are rejected even if accidentally authorized. The original template's `## Child issues and delivery plan / 子任务与交付计划` heading is also recognized. This applies to GitHub and local tasks, automatic selection and authorization for explicit-ID execution, resume and submission. A child's `Parent: #N` link does not affect eligibility; do not declare its tracking parent as a prerequisite that must be completed first. Maintainers must mark custom plans without these indicators; `init --sync-github` preserves existing templates rather than upgrading them.

Existing label names, colors and descriptions, template files and other forms are preserved. Preserved project templates retain their own defaults and should be reviewed separately. Add project-specific area labels as needed. For already-onboarded projects or recovery after network/permission errors:

```bash
devrelay --project /path/to/project init --sync-github
```

This uses existing configuration without overwriting configuration, ignore rules or templates, adding only missing items. Custom `[queue]` authorization, priority and blocker labels are honored and described in generated templates; a type label reused as a queue control is not pre-applied. Uncertain creation responses are read back before continuing. Only complete template files are published, so interrupted writes can recover missing files. Required archived labels or queue-label spelling mismatches require a maintainer to restore the label or align configuration.

### Selection and delivery

Tasks must be open, carry `auto:approved`, exactly one `priority:P0/P1/P2`, no `blocked` / `needs-input`, and exactly one declaration: `Depends on: none` or `Depends on: #12, #27`. Dependencies must be closed and linked to merged PRs whose merge commits are ancestors of the refreshed target branch. Existing worktrees and explicitly associated open PRs prevent duplicate claims.

```bash
devrelay next
devrelay start 12
devrelay prepare 12
devrelay status
devrelay check 12
devrelay submit 12
```

`next` explains selection and exclusions without claiming work; it reads GitHub and fetches the base. Engineering tools and optional orchestration share the same task authorization, dependency and acceptance rules. An agent report does not replace current evidence produced by `check`.

`submit` requires current verification evidence, commits, pushes and creates or updates a **draft PR**. `run --submit` combines the steps. A GitHub remote must resolve to exactly one fetch URL and one push URL, both targeting `project.repository`; SSH and HTTPS may be mixed. Multiple fetch/push URLs are rejected instead of allowing Git push fan-out. To bind the actual endpoint, `insteadOf` / `pushInsteadOf` URL rewrites affecting that remote are also rejected explicitly; configure the GitHub URLs directly. Git configuration is validated from the worktree that performs each transport, so branch- or path-conditional `includeIf` configuration cannot bypass the check. Submission and cleanup use an absolute-path transport argument that cannot select a named remote, bound to the validated URL in the same Git invocation. Neither a rewrite nor a URL-named remote alias added after validation can redirect or fan out the transport; existing credential and CA settings remain available. Submission records the configured remote/merge tracking settings directly even for single-branch clones, and updates the tracking reference selected by the existing fetch refspec rather than assuming `refs/remotes/<remote>/<branch>`. Cleanup uses the same mapping. By default delivery stops at draft; opt into review and merge below. PRs use `Refs #N`, preserving issue scope. DevRelay does not close whole issues, release, deploy or create background schedules.

When a remote targets a different repository, the error identifies the fetch or push URL with `does not match project.repository`; correct that remote before retrying GitHub operations.

Tracking updates after push and tracking cleanup use the first matching positive fetch refspec. Negative fetch refspecs restrict fetching, but do not cancel this tracking mapping.

Transport preserves Git's `GIT_CONFIG_NOSYSTEM` boolean policy: `0`/`false` keeps the original system settings, while `1`/`true` disables them. Other Git boolean spellings are supported; invalid values stop the transport.

## Optional mode: Codex orchestration

Explicitly invoke these commands when DevRelay should launch the implementation agent:

```bash
devrelay doctor --with-codex
devrelay run 12             # Setup → Codex implementation → acceptance
devrelay run 12 --submit    # Then deliver under the project policy
devrelay resume 12          # Resume the saved DevRelay implementation session
```

`run` handles one task, recovering interrupted work first when no ID is given; failed or blocked work returns `needs_attention` until explicitly addressed. Codex implements in `.devrelay/worktrees/ID`; DevRelay runs configured checks. Failed checks feed back into the same saved Codex session within `max_attempts`. External blockers are retained.

Do not let no-ID `run` or `watch` take over tasks still being edited by an external agent. Stop the previous executor and inspect the worktree before switching modes. Both paths use the same authorization, dependency, verification and delivery gates; an agent report does not replace current verification evidence.

### Continuous issue dispatch

Run from an onboarded GitHub project:

```bash
devrelay --project /path/to/project watch
# Change the empty-queue interval, or stop after delivering two PRs:
devrelay --project /path/to/project watch --poll-interval 30 --max-tasks 2
```

`--interval` is a shorter alias for `--poll-interval`, for example `watch --interval 30`; both accept positive integers and default to 60 seconds.

`--limit` is a shorter alias for `--max-tasks`, for example `watch --limit 2`; both accept positive integers, with no task limit by default when omitted.

`watch` serially claims authorized issues with integrated dependencies, ordered by configured priority and creation time. It implements, verifies, submits a draft PR and runs enabled independent read-only review. `[delivery] auto_merge` in the project's `.devrelay/config.toml` explicitly selects the delivery mode; it defaults to `false`, and the CLI does not enable authorization:

- **Manual merge** (`auto_merge = false`): successful draft submission and enabled review allow the next issue immediately. Drafts remain drafts, and submitted tasks are not implemented again.
- **Automatic merge after acceptance** (`auto_merge = true`): independent review is mandatory. After evidence, CI, GitHub reviews and branch protection pass, request an exact-head merge, wait for GitHub's `MERGED` readback, rerun post-merge setup/checks and finish configured cleanup. Only `completed` tasks allow the next claim. Pending CI, required human approvals and protection conditions are polled at `--poll-interval`; no other issue is claimed during the wait.

An empty queue is also checked at `--poll-interval` (default 60 seconds), so newly authorized issues can be picked up. Existing authorization, blockers, dependencies, duplicate-claim and acceptance gates still apply. See the configuration example below; editing configuration alone does not start an executor.

The command runs in the foreground; keep the terminal and machine running. It does not install a background service or startup job. Output is flushed JSON Lines events followed by a final status: `watch_started.mode` is `manual_merge` or `auto_merge`, and `merge_waiting` events explain waits. `--max-tasks` counts deliveries completed during this invocation, including recovered deliveries: successful PRs in manual mode, tasks passing post-merge acceptance and cleanup in automatic mode. Omit it to keep watching.

The repository worker lock remains held during empty-queue, CI and merge-queue waits. `status` / `next` remain available; stop `watch` before other DevRelay mutations. `Ctrl+C` or `SIGTERM` retains work and exits 130. Restart prioritizes interrupted implementation, submission and delivery. Automatic mode reads back existing PRs, reuses current review evidence and recovers queued merges, post-merge acceptance and cleanup. A successful review process with complete saved evidence recovers its result directly; incomplete review evidence becomes `review_blocked`, requiring explicit `review ID` without silently repeating review. Manual-mode PRs remain for maintainer integration.

Acceptance failures, external blockers, rejected reviews, failed/skipped CI, unresolved review feedback, conflicts, revoked authorization, stale evidence, network/API errors and configuration changes stop dispatch and preserve worktrees/logs. Missing or unfinished CI and pending protection conditions keep waiting. Inspect `devrelay status`, explicitly recover through `resume` / `check` / `submit` / `review` / `merge`, then restart `watch`; `post_merge_failed` and `cleanup_failed` are not retried automatically. Each new task starts from the latest target base; dependencies still require closed issues and integrated linked delivery PRs.

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

Both automatic switches default to `false`. Review alone retains draft status. Automatic merge includes review, marks an approved draft ready and requests squash merging when gates pass. Named required checks prevent missing or not-yet-created CI from being ignored; all reported checks must also succeed. Skipped, neutral, failing and pending checks do not pass. No CI evidence blocks by default. Explicitly set `allow_no_ci = true` only for projects without GitHub CI; local acceptance, AI review and any named required checks still apply.

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

After merge, DevRelay fetches the base, verifies commit ancestry and tree equality, then runs every setup/check command in a fresh detached worktree. Only successful acceptance records `completed`. API timeout recovery reads GitHub first and never re-merges an already merged PR. Unexpected trees or failed post-merge checks record `post_merge_failed`, retain evidence and failed worktrees, and block automatic dispatch without an ID. GitHub's head condition does not atomically lock the base, so pre-merge refresh and post-merge tree verification both matter. A post-merge failure needs maintainer remediation; DevRelay does not automatically revert a completed remote merge.

To revise a PR, fix its preserved worktree and run `check ID`, then `submit ID`. New commits invalidate old reviews. Configuration changes require fresh acceptance too. Maintainers still own issue closure and dependency delivery links.

### Post-merge cleanup

`cleanup_worktrees` and `cleanup_branches` default to `false`; opt in before running tasks. Worktree cleanup removes the successful task's implementation checkout. Branch cleanup removes its local, remote and remote-tracking branches and requires worktree cleanup too. Enable only worktree cleanup to retain branches.

Cleanup requires a merged PR, its merge commit in the refreshed base, and successful post-merge setup/checks. Failed acceptance retains the task branches, implementation checkout and failed acceptance worktree. Even after a successful retry, previous failed worktrees remain listed in `retained_worktrees`. Successful temporary detached acceptance worktrees are reclaimed as before.

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

All Git operations that generate source evidence disable local replacement refs and inspect actual commit objects, leaving replacement refs untouched. NUL-delimited paths are parsed as bytes, supporting non-UTF-8 filenames and carriage returns where the filesystem permits them.

Untracked submodule files are detected using only `.gitignore` rules, without local `info/exclude` or `core.excludesFile` rules. Detection and isolated-index comparisons force `core.ignoreCase=false`, so a rule such as `build/` cannot hide `BuIlD/undelivered.txt`; user configuration is left unchanged. Staged content is compared using a read-only index operation; source evidence does not rebuild cache trees in real submodule indexes and disables optional index refresh writes.

Tracked regular files in submodules also have their raw bytes compared against HEAD blobs, bypassing attributes and clean filters so local transformations cannot hide undelivered content. Submodule worktree bytes must match the commit; checkouts that require line-ending normalization, encoding conversion or smudge filters to match committed blobs block acceptance.

Committed symlinks in submodules must remain symlinks, and their raw `readlink` target bytes must match HEAD blobs. Acceptance blocks when `core.symlinks=false` materializes a link as a regular file or hides such a replacement; verify in a worktree that supports symlinks.

Clean committed submodule HEAD changes are included in the source tree through the parent gitlink and can be delivered normally; submission explicitly stages checked-out gitlinks independently of submodule ignore configuration. An uninitialized submodule path must be absent or empty; residual files block acceptance. Projects whose checks need submodule contents should initialize them explicitly in `[[setup]]`. Source/history/base/configuration/requirements drift invalidates evidence. Integrate a newer base manually and rerun checks; existing work is never reset automatically.

SQLite state, a repository-wide worker lock, command logs, resource receipts, verification and Codex JSON events live under `devrelay/` in the Git common directory. Every attempt gets a new directory. DevRelay records that directory and event-log path before launching Codex, then persists the task's session ID as soon as it receives a valid `thread.started`. Before invoking Codex, `devrelay resume ID` validates the saved ID's format and cross-task ownership as well as the previous log, then resumes that exact session, never `--last`. An empty pre-launch log is safe to retry; incomplete, invalid or conflicting session events block recovery.

Ctrl+C and controller crashes cannot silently switch recovery to a new session. On timeout or interruption, DevRelay sends SIGTERM to the entire command process group, then sends SIGKILL if non-zombie members survive the bounded grace period and waits a bounded time for exit. It clears `active_process` only after the group is gone; a group that cannot be stopped retains the marker and blocks another worker. Every command launch, including automatic retries within the same worker, checks the previous marker. If that group is alive or a bounded inspection cannot confirm its exit, the launch is refused and the original marker is preserved.

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

The optional self-maintenance executor and AI reviewer are pinned to `gpt-6.1-sol`, verified with Codex CLI `0.162.1`. Upgrade an older CLI if it rejects this model. Changing the model configuration requires fresh acceptance and review of existing tasks. The external agent's model is controlled by its host.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Tests use real temporary Git repositories and a fake Codex CLI. Delivery tests push and squash-merge in temporary repositories, run fresh post-merge checks, and mock GitHub review/CI APIs and timeout recovery. They do not call models or push public repositories. Real target projects still require their own acceptance. See [architecture](docs/architecture.md) and [official Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive).

Non-UTF-8 filename end-to-end cases run on supporting filesystems. Only fixture creation that explicitly rejects an invalid UTF-8 name with `EILSEQ` reports a platform skip, which is not successful product acceptance. Git tree/index byte-path regressions run without checking out these names on every platform; permission and product errors remain test failures.
