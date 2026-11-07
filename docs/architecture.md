# Architecture / 架构

## Agent-driven tools / Agent 驱动的工程工具

The primary caller is an external agent. It owns planning, exploration, implementation,
interpretation of evidence and session recovery. It calls `next`, `start`, `prepare`,
`check` and `submit` as separate engineering operations. `prepare` runs configured setup
without starting an implementation agent; each call invalidates previous verification
and preserves setup logs. Failed setup is explicitly retried. `check` and delivery use
the same evidence and authorization gates for every caller.

Configured setup is required under the current configuration at check and delivery
gates. Unmerged tasks can reprepare after PR identity readback, invalidating verification,
review and merge-gate records while retaining their logs. Publication rechecks mutable
requirements at each boundary. Local completion binds the latest accepted source to
the actual delivered commit and its integration, rather than an older ancestor alone.

主要调用方是外层 Agent，负责规划、探索、实现、解释证据和自身会话恢复，逐步调用
`next/start/prepare/check/submit`。`prepare` 执行项目 setup，不启动实现 Agent；每次调用
使旧验收失效并保留准备日志，失败后明确重试。所有调用方共用验收与交付门禁。

配置了 setup 时，验收与交付门禁要求当前配置下的成功准备。未合并任务可在 PR 身份
回读通过后重新准备，使验收、审查和合并门禁记录失效但保留日志。发布边界重新比较可变
要求；本地完成绑定最新验收源码、实际交付提交及集成结果，不能仅凭旧提交的祖先关系。

`work/run/resume/watch` are optional adapters that launch Codex and manage its implementation
session. They are explicitly invoked, not required for engineering tools. AI review is
also optional and currently uses Codex. `doctor` only checks Codex when requested with
`--with-codex` or required by enabled automatic delivery. No planning scheduler is introduced; Issues and task files carry plans and requirements.
An explicit workspace registers its task once in workspace state; member repository
state stores reservations, not duplicate task requirements.

`work/run/resume/watch` 是显式调用的可选 Codex 编排，管理它启动的实现会话；工程工具不依赖
该路径。可选 AI 审查当前也使用 Codex。`doctor` 仅在指定 `--with-codex` 或启用自动 AI
交付时检查 Codex。计划与要求由 Issues 和任务文件承载，不增加规划调度器；多仓任务只在工作区状态登记一次，各仓状态只保存占用。

## Responsibilities / 职责

`triage` implements automatic authorization for controller-created discoveries by
default (`discovery.auto_triage=true`), and all Issues when `queue.mode=autonomous`.
Both external agents (`--prepare`, `--file`) and managed Codex investigate in a
detached base checkout; controller-configured setup and retained prompts/reports
share the repository lock. Context hashes bind the task, policy, setup/checks,
base and investigation attempt. A fresh `ready` decision of an allowed kind joins
the existing queue; a configured explicit approval label remains an override of
automatic scope, never of plans, dependencies or delivery checks. Scope judgments
are agent reports, not independent acceptance. Verification and review bind the
actual triage result as well as the original requirements. Triage session IDs are
reserved independently of implementation and review, including failed runs.

Human Issues retain default `manual` authorization. Automatically processed tasks park only known local
input/repair-budget handoffs without pending merge mutations; unknown state and
repository failures still block dispatch. Issue-body decisions re-trigger triage
and resume the original implementation session. Unchanged negative decisions are
cached. A persistent dispatch budget charges before triage/work, including failures;
CI polls and process restarts do not refill it. `watch --reset-budget` explicitly
archives and renews the budget. Manual projects charge only automatic discovery
tasks; autonomous projects charge every dispatched task. There is no additional
service or state database.

项目自动模式共用分诊与授权门禁：外层 Agent 和托管执行器都先在独立 base 工作树调查。
范围、Issue、环境准备和验收变化会使旧决定失效；自动验收还绑定实际分诊范围。
已知任务级待决/返工耗尽可以暂停并释放串行 worker；未知状态、准备故障和未确认合并
仍停住整个队列。正文补充决定后重新调查，使用原实现会话继续。调度预算持久保存，
显式重置才补充，不新增服务或数据库。

`workflow.work` is the shared continuation path for `work`, `run --submit`,
`resume --submit` and `watch`. Low-level tools remain independent and do not launch
implementation agents. The loop spends a durable implementation-turn budget before
each launch, feeds check/review/CI evidence back to the original implementation
session and reviews changed commits in fresh read-only sessions. A current negative
review remains blocking even when a repair produces no new commit. Restarts do not
replenish budgets; explicit retry records a new budget while retaining history.

`workflow.guidance` exposes the same repair and handoff distinctions to external
agents through `status.next_action`. A clean base advance can merge into the task
branch and revalidate. Dirty work, conflicts, incomplete reviews, missing resources,
unknown CI states, GitHub human decisions and exhausted budgets preserve the scene.
Uncertain merge mutations retain the dedicated explicit retry protocol.

统一闭环负责有限返工和任务续跑，基础工具保留外层 Agent 自主权。人负责范围、授权与
无法自动解决的输入；实现 Agent 根据证据修复；独立会话审查；控制器执行项目验收及
交付门禁。无需新增服务、插件依赖、任务数据库或多 Agent 调度平台。

Configuration receipts use stage fingerprints: setup includes project/setup; checks
adds acceptance commands; review adds reviewer executable/model/profile/timeout.
Delivery changes invalidate queued mutation policy, not earlier acceptance. Full
configuration identity is still checked during each operation. Legacy evidence
falls back to exact full-config matching. Optional `post_merge_checks=false` skips
only the second setup/check run, never ancestry or exact merged-tree checks.

| Module | Responsibility / 职责 |
|---|---|
| `config` | Validated TOML, command discovery / 配置校验与命令识别 |
| `onboarding` | Add missing GitHub labels and safe issue templates / 补齐标签与不预设执行授权的模板 |
| `providers` | Local and GitHub snapshots, priorities, dependencies / 任务快照、优先级与依赖 |
| `triage` | Isolated investigation, evidence and project-level authorization / 隔离调查、证据与项目级授权 |
| `discovery` | Evidence-backed follow-up intake, deduplication and unapproved publication / 新问题报告、去重与待授权任务发布 |
| `git` | Worktrees and source hashing / 工作树与源码哈希 |
| `state` | Shared SQLite state and POSIX single-worker lock / 状态与单 worker 锁 |
| `codex` | Optional implementation/review adapter, explicit sessions and structured outcomes / 可选实现与审查适配器、精确会话与结构化结果 |
| `process` | Timeout, process groups and unique receipts / 超时、进程组与独立回执 |
| `runner` | Agent-callable claim/setup/check/submit plus optional implementation loop / Agent 工程操作与可选实现循环 |
| `workspace` | Explicit multi-repository tasks, joint acceptance, reservations and linked drafts / 多仓任务、联合验收、占用与关联草稿 |
| `worker` | Optional serial Codex dispatch with configured delivery and recovery / 可选串行 Codex 调度、配置的交付策略与恢复 |
| `workflow` | Shared bounded task continuation and actionable handoffs / 统一任务闭环、有限返工与下一步指引 |
| `delivery` | Fresh AI review, GitHub merge gates, post-merge acceptance / 审查、合并门禁及合并后验收 |
| `cleanup` | Opt-in exact-ref cleanup after acceptance, durable recovery / 验收后清理与恢复 |

## Task lifecycle / 任务生命周期

```text
Primary tools / 主要工具:
queued → claimed → preparing → prepared → [external agent edits] → verifying → verified
                       ↓                                             ↓          ↓
                 failed/interrupted                                failed   committed → pr_open

Optional Codex orchestration / 可选 Codex 编排:
queued → claimed → preparing → prepared → implementing → verifying → verified
                                   ↑             ↓           ↓         ↓
                                resume        blocked      failed   committed → pr_open
                                                                         ↓
                                                              completed (local, integrated)

pr_open → reviewing → reviewed → merge_waiting → merging → merged → cleaning → completed
              ↓                                            ↓          ↓
        review_blocked                               post_merge_failed cleanup_failed

merge_waiting / merging → merge_blocked (revocation, stale evidence, uncertain request)
merge_blocked → explicit recovery / merge --retry-request (fresh gates, no remote queue)
```

State is saved before long-running phases. Main-checkout configuration controls commands even when a task changes its own copy. Verification is independent of any agent's completion report. Optional Codex resume preserves code and targets only the saved session. External sessions remain owned by their host. GitHub remains authoritative for live Issue requirements, labels and integrated dependencies; autonomous authorization also requires the configured project policy and a current diagnosis.

长时间运行前保存状态；主 checkout 配置控制实际执行命令。Agent 的完成报告不等于验收通过；可选 Codex 编排的恢复保留代码且只使用该任务会话，外层会话由宿主管理。GitHub 提供实时 Issue 要求、标签及依赖交付状态；自动授权还要求项目策略和当前有效分诊。

## Boundaries / 边界

Discovery is enabled by default during existing implementation/review turns or via the explicit
`discover` tool, not a new planner or audit service. A valid report batch and its
source observation are persisted before intake or publication; cumulative-limit
failures retain pending reports. Issue markers and normalized titles prevent exact
duplicates, including across source tasks and uncertain-write recovery. Semantic
deduplication is not guaranteed. Creation intent is saved before the GitHub mutation;
an uncertain request is read back and needs explicit `--retry-create` before resending.
New tasks receive no execution label. Controller-created discoveries default to
independent investigation and PR review, even with manual project authorization and
global auto-review disabled. Both source policies can be explicitly disabled; global
autonomous/review/merge settings still apply. Source recognition requires a locally
retained published receipt for the same provider/repository and Issue ID plus its
marker; body text or bot authorship alone cannot grant authorization. Existing human
Issues matched by title retain their policy. Publication failures are visible in status and watch events but do not change
the source task's verification or review verdict. Discovery reports are agent claims,
not independent evidence of correctness. No additional runtime dependency or database
is introduced.

发现链路复用当前 Agent 回合和任务状态，先保存报告与观察时的源码，再去重发布。超过
单任务上限的报告保留待处理；创建结果不确定时先回读，明确重试才能重发。新任务不自动
添加授权标签；控制器创建且保留来源记录的发现任务默认独立分诊，符合范围后执行并自动
审查 PR，人工模式也适用。已有人工 Issue 和自动合并保持原策略，共用交付门禁。
发布错误独立呈现，不能改变原任务的验收与审查结论。

- One controller, one worker per Git repository; POSIX lock, no distributed leases.
- In agent-driven mode the lock covers each tool call, not external editing between calls. The host must serialize editors and optional orchestration for a repository. External agent sessions are not stored or resumed by DevRelay.
- GitHub onboarding creates only missing repository labels and local template files under the worker lock. It never assigns issue labels or authorization. Existing files/definitions remain intact; uncertain creates are read back and `init --sync-github` repairs partial setup. Templates become public only after normal Git delivery to the default branch.
- One issue delivers one reviewable result; split children only for independent value or acceptance criteria. Templates and implementation/review prompts state this scope policy. Queue eligibility and explicit-ID authorization reject tracking parents marked `scope:plan`, `Task kind: plan`, or the original plan-template child-delivery heading, even with execution approval. This is marker-based enforcement, not semantic classification of arbitrary issue prose. / 一个 Issue 对应一个可审查结果，仅有独立价值或验收标准才拆子项；模板、执行和审查提示词约束粒度。队列与显式 ID 授权检查拒绝已标记的总体计划父任务，即使误加执行授权；检查依据明确标记，不自动理解任意正文。
- Each task has bounded attempts and command timeouts; explicit resume may grant a new bounded attempt window. `watch` is a foreground loop holding the repository worker lock through execution and idle waits; manually authorized human tasks stop on errors or attention states, while automatic discoveries and autonomous projects park known task-local handoffs and reuses the existing evidence and authorization gates. Manual mode advances after draft submission/review; explicit project `auto_merge=true` holds the current task through CI/protection/queue waits, confirmed merge, post-merge acceptance and cleanup. Completion or an explicitly parkable autonomous handoff permits the next claim. Unclassified CI/review failures stop watch; pending conditions poll without repeated development, review or merge requests. Configuration changes stop it before further dispatch.
- Default Codex sandbox is workspace-write; approval policy is never, so permission failures become actionable failures rather than unattended prompts.
- Implementation prompts distinguish development-sandbox validation limits from actual implementation blockers. A ready report with sandbox diagnostics still runs every original host check; blocked reports and failed host checks remain barriers to delivery.
- Session ownership accepts the fixed historical integration-review receipt layout only for completed tasks with exact cached receipt, PR and HEAD matches, without symlinks. This reserves the historical review session and preserves its evidence; arbitrary out-of-scope paths remain rejected.
- Delivery defaults to draft PR. Project configuration may opt into fresh read-only AI review and exact-head squash merging; GitHub human approvals and protection still apply.
- Review binds PR URL, head, base, tree, configuration and task requirements; CI is read from the exact head commit. Truncated API data fails closed. Default no-CI policy blocks, with an explicit project opt-out.
- Queue behavior follows the [GitHub CLI merge contract](https://cli.github.com/manual/gh_pr_merge); cancellation uses [GitHub GraphQL pull request mutations](https://docs.github.com/en/graphql/reference/pulls).
- A completed review process receipt plus identity-bound review checkpoint can be read after a crash without a second review session; incomplete evidence requires explicit review. A durable `merge_request` precedes the exact-head mutation. Queue/remote-auto-merge readback prevents duplicate requests; uncertain/disappeared requests require explicit `merge --retry-request` after fresh gates. Older queued tasks without a request checkpoint fail closed.
- On evidence/configuration/authorization failure, recorded pending requests are cancelled through GitHub GraphQL and read back; cancellation failures retain evidence and block dispatch. Interrupts preserve queued requests. Remote queue execution is independent of the controller: cancellation and authorization checks are not atomic with merge, and an already merged outcome must be retained. 合并前持久化请求，排队只回读；不确定结果需显式重试。撤销授权或证据变化时尝试撤销远端排队请求，失败保留错误；中断保留队列，远端合并与撤销仍存在竞态。
- Review recovery validates canonical task-owned run paths and rejects symlinks at directory and file boundaries. The process receipt must match the successful configured read-only command, worktree and log; result/approval paths stay in that run. 恢复只读取实际归属本任务的证据；目录或文件符号链接、进程回执身份不匹配时阻断，保留现场并要求显式审查。
- GitHub HEAD matching is atomic, base matching is not. Refresh before merge and verify the merged tree afterwards. Enabled post-merge setup/checks run in a separate detached worktree; failure preserves the merge checkpoint and blocks new automatic dispatch.
- Cleanup defaults off for task resources. Successful acceptance scratch trees are removed; failed trees/evidence remain. Optional task worktree and exact-head branch cleanup checkpoints each step; recovery reads actual refs without assuming the original checkout still exists.
- Remote deletion uses an explicit expected SHA lease; local/remote-tracking ref deletion uses compare-and-swap. Worktree removal is never forced. Git operations outside DevRelay still require coordination with the repository worker.
- Completed GitHub tasks stay claimed in shared state even after their branches/worktrees are removed, preventing repeat dispatch of still-open scoped Issues.
- Explicit multi-repository workspaces reuse per-repository locks, source hashing, command execution and Codex sessions. A workspace receipt binds all participating sources and draft PR checkpoints; see [workspace MVP](workspace.md).
- No OS service installer, remote compute adapter, automatic conflict resolution, release adapter or atomic cross-repository transaction. Continuous issue dispatch is explicitly started with `watch`; it does not create background schedules.
- Project commands may require services, credentials or hardware; DevRelay does not provision them.
- Process markers detect remaining foreground groups after controller crashes. Commands must not daemonize or escape the group. There is a brief spawn-to-marker crash window; this is cooperative recovery, not an OS isolation guarantee.

默认单仓库、单 worker、有限重试并停在 draft PR；项目可明确开启审查与合并。`watch` 持续串行调度，空队列等待期间也保留 worker 锁，人工模式在 draft 交付后继续；明确开启 `auto_merge=true` 后等待 CI、保护条件及合并队列，合并后验收和清理完成才继续。人工授权的人提任务遇到失败或人工处理状态会停止调度；自动发现任务和项目自动模式可以暂停已知任务级待决，继续其他独立任务。审查绑定准确提交与 base；CI、GitHub 审查意见和保护条件另行核验。默认合并后在独立工作树重跑 setup/checks；可由项目配置省去这轮命令，但仍核对合并身份与源码树；失败保留远端已合并的事实和本地证据。后台服务安装、远端执行、多仓库事务与发布留给后续适配。项目命令负责业务环境。进程组恢复要求命令不脱离前台进程组；进程启动与标记之间仍有短暂中断窗口，不能宣称操作系统级隔离。

## User project registration

`project_registry` owns the optional user catalog, separate from repository configuration and task/workspace authorization. The dashboard and `projects add/locate` use the same registration logic. Read-only proposals bind repository/controller identity, configuration, ignore rules, saved state and catalog contents. Writes take the catalog lock then the repository worker lock; retries preserve any completed configuration and metadata writes. Catalog replacement is atomic with a byte-for-byte backup. A failed catalog write may leave local registration complete; repeat the operation without deleting state.

Repository identities are retained in Git-common-directory metadata and catalog entries. Existing worktrees deduplicate to the registered controller. Missing paths remain visible for explicit relocation. Live identity copies, identity changes and controller relocation with task history fail closed. Initialization never runs setup commands, tasks or models; GitHub label/template setup is a separate revision-checked, locked action. Installation diagnostics expose the actual runtime and reject missing controller registration as not ready.

The wheel smoke test uses the public installed CLI and loopback API from outside the source tree, with two local repositories, an isolated catalog, no `PYTHONPATH`, and a reinstall. CI runs it on supported POSIX platforms and minimum/current tested Python versions.
