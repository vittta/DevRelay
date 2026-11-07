# Agent workflow / Agent 工作流

DevRelay is an engineering toolkit for an external agent. The agent decides what to
investigate, how to implement and how results affect the next task; the tools enforce
project authorization, dependencies, source-bound acceptance and delivery policy.

DevRelay 为外层 Agent 提供工程工具。Agent 决定调查方向、实现方式和结果对后续工作的
影响；工具执行项目授权、依赖、绑定源码的验收和交付策略。

## Plan and explore / 规划与探索

- Inspect the applicable AGENTS.md, owner code, existing Issues, PRs and evidence.
  State unknowns and propose alternatives before turning a goal into tasks.
- Use a tracking parent for an overall plan (`scope:plan` or `Task kind: plan`).
  Each executable child delivers one observable result with its required code,
  configuration, tests and documentation. Split only independently valuable outcomes.
- An exploration task declares its question, resource/time budget, reproducible
  evidence and stopping condition. A negative experimental result can satisfy the
  task when the agreed acceptance requires faithful execution and complete evidence.
- Publish tasks and grant execution labels only within the maintainer's authorization.
  Planning does not itself authorize execution or a change in acceptance. Local
  `task add` explicitly authorizes its task; use it only for already authorized work.
- Update the plan from actual results. Keep superseded conclusions and failed trials
  traceable. A change in scope, requirements or acceptance needs the corresponding
  authorization and new verification.

先读项目指令、所属代码、已有任务和证据，明确未知项与候选方案。总体计划只跟踪子任务；
可执行子任务按独立结果划分，包含该结果所需的代码、配置、测试与文档。有界探索明确
问题、资源与时间预算、复现证据和停止条件；负结果也可满足如实执行与完整证据的验收。
发布和授权均服从维护者已有授权，计划本身不授权执行；本地 `task add` 会直接授权任务。
根据实际结果更新计划并保留历史结论与失败尝试，范围或验收变化需对应授权及重新验证。

## Engineering loop / 工程循环

In `queue.mode=autonomous`, `next.triage_candidates` identifies unapproved Issues
needing investigation. Use `triage ID --prepare`, read `prompt.txt` and
`result-schema.json` in the returned `triage.context.run`, investigate in
`triage.context.worktree`, then submit `triage ID --file RESULT.json`. Echo the
context hash. No model is launched by these two tools. The controller validates
the report and applies project policy; `ready` admits the normal engineering loop,
`needs_input` asks concrete questions, and `skip` retains a non-executable diagnosis.
Do not add an approval label yourself to bypass a rejected diagnosis. Managed
`triage ID` / `work` / `watch` use the same gate. Put human decisions into the Issue
body; comments are not consumed. Changed requirements and policy expire the decision.

自动模式下先读取 `next.triage_candidates`，用 `triage ID --prepare` 准备独立调查工作树，
按返回目录中的 prompt 和 schema 调查后提交结果。控制器按项目策略判断能否进入实现，
不需要外层 Agent 自己给任务加授权标签。`needs_input` 提供明确待决问题，`skip` 保留
诊断但不自动关闭 Issue。人把决定写入正文后可重新分诊；内容和策略变化使旧结论失效。

Run commands from the project controller checkout, keeping `--project` explicit when
working elsewhere. Parse JSON output; use the returned `worktree`, not a guessed path.

从主 checkout 调用；在其他目录操作时显式指定 `--project`。读取 JSON，并使用返回的
`worktree`，不要猜工作树路径。

```bash
devrelay --project /path/to/project doctor
devrelay --project /path/to/project status
devrelay --project /path/to/project next
devrelay --project /path/to/project start 12
devrelay --project /path/to/project prepare 12
# External agent investigates and edits in the returned worktree.
# 外层 Agent 在返回的工作树中调查和实现。
devrelay --project /path/to/project check 12
devrelay --project /path/to/project submit 12
```

| Tool | Agent action / Agent 的处理 |
| --- | --- |
| `status` | Inspect existing work before selecting another task / 先检查已有现场 |
| `next` | Read the suggested eligible task and exclusion reasons; does not claim / 读取候选与排除原因，不领取 |
| `start ID` | Claim or recover a worktree; no setup or model call / 领取或恢复工作树，不准备环境、不调用模型 |
| `prepare ID` | Run configured setup, retaining logs; every invocation invalidates old checks / 执行配置的环境准备并保存日志，每次使旧验收失效 |
| `check ID` | Run acceptance and inspect failures; repair then recheck / 执行验收，失败时读取证据、修复并重验 |
| `submit ID` | Require current evidence, then deliver under project policy / 用有效证据按项目策略交付 |
| `triage ID --prepare` / `--file RESULT.json` | Investigate and report under automatic project policy / 按项目自动策略调查并提交结论 |
| `discover ID --file reports.json` | Report follow-ups into the project's manual/automatic intake / 上报独立新问题，进入项目授权或分诊流程 |

Preparation operates on a claimed, unfinished task, including a
local commit or an open PR, and never starts an agent. Existing PR identity and state
are checked; closed, merged, actively merging and mismatched tasks are rejected.
Explicit calls rerun setup, so project setup commands should support retries. Fix
failed or interrupted setup and retry `prepare`. When no setup is configured, it
records preparation without running a command. It never resets the worktree.

Configured setup must have succeeded for the current configuration before acceptance
or delivery. Repreparation invalidates verification, AI review and merge gates while
retaining their historical evidence. After environment changes integrate the target
base, prepare, check and submit again; reuse the original PR. Submission rechecks
mutable requirements at publication boundaries and preserves partial delivery on error.

环境准备作用于已领取且未完成交付的任务，包括本地提交和开放 PR，不启动 Agent；已有 PR
会核对身份和状态，已关闭、已合并、正在合并或身份不符时拒绝准备。显式调用会重跑 setup，项目命令
应支持重试；失败或中断后修复原因，再运行 `prepare`。未配置 setup 时仅记录准备状态。
它不重置工作树。

配置了 setup 时，必须在当前配置下成功准备后才能验收与交付。重新准备会使验收、AI
审查和合并门禁失效，历史证据保留。环境变化后集成目标分支，再准备、验收、提交并复用
原 PR。提交在发布边界重新比较可变要求；发生错误时保留已经完成的交付步骤。

Submit defaults to a draft PR. Enabled automatic review/merge may invoke Codex and
advance delivery even in agent-driven mode. Explicit `review` also invokes Codex;
`doctor --with-codex` checks that dependency. Existing human approval and GitHub branch
protection requirements remain applicable. Local projects use `submit ID --local`,
then `task complete ID` after integration into the configured base.

Local completion checks that the latest accepted source is the delivered commit,
the worktree is unchanged, requirements/configuration still match, and the commit is
integrated. A new check alone does not integrate new content or unlock dependencies.

提交默认停在草稿 PR；即使由外层 Agent 驱动，已启用的自动审查或合并策略仍可能调用
Codex 并推进交付。显式 `review` 也调用 Codex，用 `doctor --with-codex` 检查其依赖。
既有人工批准和分支保护要求仍适用。本地项目用 `submit ID --local`，集成到目标分支后
再用 `task complete ID` 标记完成。

本地完成会核对最新验收源码与交付提交一致、工作树未变化、要求与配置一致且提交已集成。
仅重新验收不能交付新增内容或解锁依赖。

## Discover follow-ups / 发现后续任务

During implementation or QA, record independently actionable out-of-scope bugs using
`discover SOURCE_ID --file reports.json`. Each JSON-array entry has `title`,
`description`, `evidence` and `acceptance`; include actual reproduction or precise
code evidence, never credentials or private data. Keep current regressions, missing
acceptance and unfinished scope in the original task. A report is not a passed check.
The controller deduplicates and publishes the task, retaining source provenance;
it never grants an approval label. Apply authorization only within the maintainer's
existing authority. Controller-created discoveries default to independent triage
(`discovery.auto_triage=true`) even in manual projects; their PRs default to automatic
review (`discovery.auto_review=true`). The existing global merge switch still applies.
Opting out of discovery triage in a manual project restores per-task approval.
An existing human Issue matched as a duplicate does not inherit this source policy.

实现或 QA 中发现有证据且范围独立的新问题时，用 `discover 来源ID --file reports.json`
上报。JSON 数组每项包含 `title`、`description`、`evidence`、`acceptance`；记录实际复现
或精确代码证据，不包含凭据与私密数据。本次回归、未完成范围和缺失验收仍留在原任务。
控制器去重、创建任务并保存来源，不添加授权标签；新建的发现任务默认独立分诊，符合范围
后执行并自动审查 PR，人工模式也适用。合并仍由全项目开关控制。重复匹配到的已有人工
Issue 不继承此来源策略；显式关闭发现自动分诊后，人工模式需逐条授权。

Managed implementation/review turns collect these reports by default
(`discovery.enabled = true`); set it to `false` to opt out. No separate audit is launched. Inspect `discoveries`
and `discovery_error` in `status`. Use `discover ID` to reconcile or retry saved
reports. An uncertain creation request is not resent unless `--retry-create` is
explicit after checking GitHub. This recovery is separate from the source task's
implementation retry budget and does not weaken its delivery result.

## Recovery and optional orchestration / 恢复与可选编排

Read `status.tasks[].next_action`: actionable failed checks and review findings belong
to the implementation agent; handoffs explain missing input, evidence problems or an
exhausted managed budget. Low-level tools never silently take over implementation.
Once the external editor has stopped, `work ID` may manage continuation through checks,
bounded repair and configured delivery. `work ID --retry` explicitly opens another
budget after resolving a handoff. `run --submit`, `resume --submit` and `watch` share
this loop; `resume --submit` is an explicit retry. Review cannot replace behavioral QA:
put project-specific acceptance, including browser/end-to-end checks, in `[[checks]]`.

读取 `next_action` 获得可操作反馈：检查失败和可修复的审查意见交回实现 Agent；外部输入、
证据问题和托管预算耗尽通过 handoff 说明。停止外层编辑后，可用 `work ID` 接管任务推进，
解决原因后用 `work ID --retry` 明确开启新预算。源码、base 或要求改变须重验；配置变化
只影响对应阶段。模型/profile 改变只重新审查，不强制重装环境或重跑仍有效的本地验收。

The external host owns its agent session and a checkpoint linking the goal, task ID,
worktree and evidence. DevRelay stores tool state and receipts, not that conversation.
On recovery inspect `status`, actual files, base and evidence before retrying a tool.
Changed source, base or requirements require fresh acceptance; configuration changes
invalidate only the affected stages described above.

The repository lock covers tool execution only. It does not reserve an external
editor between calls. The host must enforce one active implementation owner and
stop live editors before handing a task to optional `run/resume/watch`. DevRelay does
not detect arbitrary external agents. `resume` resumes only a DevRelay-managed Codex
implementation session; it does not recover the external host's conversation.

外层宿主负责会话和关联目标、任务 ID、工作树与证据的检查点。DevRelay 保存工具状态和
回执，不保存外层对话。恢复先检查 `status`、实际文件、目标分支和证据；源码、base、
要求改变后重新验收；配置变化按上述阶段处理。仓库锁仅覆盖工具执行，不保留两次调用之间的编辑独占权。
宿主须保证同一任务只有一个实现者，交给可选 `run/resume/watch` 前停止原编辑者。
DevRelay 不检测任意外层 Agent；`resume` 只恢复 DevRelay 自己启动的 Codex 实现会话。

The optional adapter recovers a completed review checkpoint only from canonical, task-owned, non-symlink evidence matching the successful configured read-only process. Missing or mismatched evidence requires explicit `review ID`; this does not resume the external host’s session. Automatic merge remains an explicit project opt-in, defaulting to false.

可选编排仅从实际归属本任务、无符号链接且与成功只读审查进程匹配的证据恢复完整审查检查点。证据缺失或不符需显式 `review ID`；这不会恢复外层宿主会话。自动合并仍须由项目显式开启，默认关闭。

## Multi-repository outcomes / 多仓业务结果

Use `--workspace manifest.toml` for one authorized result spanning repositories.
Member work units share task requirements and joint verification; they are not
independent product tasks. See [workspace commands and recovery](workspace.md).

同一业务结果涉及多仓时使用 --workspace；成员工作单元共用要求与联合验收，无需按仓
拆成独立业务需求。操作与恢复见上述文档。
