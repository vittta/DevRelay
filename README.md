# DevRelay

[English](README.en.md) | 简体中文

面向 Agent 的通用工程工具：让 Agent 自主规划、探索和实现，通过 DevRelay 完成工作树隔离、项目验收与受门禁约束的交付。

主要工作流由外层 Agent 驱动，逐步调用 `next/start/prepare/check/submit`；DevRelay 管理工程状态和验收证据。`run/resume/watch` 保留为可选的 Codex 程序编排。项目负责定义安装命令与验收规则。0.4 版支持 macOS / Linux、Python 3.11+ 和 Git；核心没有第三方运行依赖。

## 多仓 autodev MVP

一个业务任务可通过显式工作区配置协调多个仓库，执行各仓检查和联合验收，并交付互相
关联的草稿 PR。`run/resume` 使用一个实现会话并在有界循环内根据检查/独立审查结果回修。
本版仅草稿交付，不自动合并。详见 [多仓使用与恢复](docs/workspace.md) 和
[工作区配置](examples/workspace.toml)。原有 `--project` 单仓工作流不变。
每轮验收保留独立且绑定摘要的日志/产物；进入独立审查后，回修必须重新获批。
部分发布按远端提交检查点恢复；已交付任务的 `submit` 只重试占用释放。
确认推送后清除其恢复意图，外部回退会阻止发布；仓内与联合检查分别保存日志，允许同名 ID。

```bash
devrelay --workspace /path/workspace.toml task add terrain-edit --title "地形局部编辑" --file /path/task.md
devrelay --workspace /path/workspace.toml run terrain-edit --submit
```


## 安装

```bash
cd /path/to/DevRelay
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/devrelay --help
```

使用 `devrelay --version` 或 `devrelay -V` 查询版本；无需在 Git 仓库内运行，也无需项目配置。

也可使用 `pipx install /path/to/DevRelay` 安装全局命令。工程工具模式不要求 Codex CLI；可选编排与 AI 审查需要安装并登录 Codex CLI。GitHub 队列和 PR 操作需要已认证的 `gh`。DevRelay 使用既有登录，不读取或复制凭据文件。

## 接入一个项目

```bash
devrelay --project /path/to/project init
# 查看并调整 /path/to/project/.devrelay/config.toml
devrelay --project /path/to/project doctor
```

`init` 创建 TOML 配置并追加运行目录的 Git 忽略规则；Python 项目同时忽略环境与安装生成物。识别 GitHub origin、默认分支、Node 包管理器与常用 scripts、Python 安装路径、Cargo 和 Go 测试入口；不会执行安装或模型任务，不覆盖已有配置或 AGENTS.md。已有 `.gitignore` 条目保留。测试脚本的 watch 模式、工作区环境与业务验收需要维护者核对。

配置中的命令均为参数数组，不经过 shell 展开；需要环境激活时写入显式项目脚本。`[[setup]]` 在工作树中运行，`[[checks]]` 定义验收。至少有一条检查才允许运行或提交任务。Python 测试框架不猜测：请明确配置实际 unittest / pytest 命令。完整例子见 [配置](examples/config.toml)。

```toml
[project]
provider = "github"
repository = "owner/project"
base_branch = "main"

[codex]
# 仅配置可选编排与 AI 审查；工程工具模式可省略此表。
sandbox = "workspace-write"
max_attempts = 2

[[checks]]
id = "test"
argv = ["python3", "-m", "unittest", "discover", "-s", "tests"]
```

将项目配置、必要忽略规则及生成的 Issue 模板纳入项目版本管理，再从主 checkout 使用 DevRelay。新建的空仓库需要先有初始提交。模型默认沿用用户 Codex 配置，可显式设置 `codex.model` / `codex.profile`。

## 主要工作流：Agent 驱动

外层 Agent 负责调查目标、探索方案、拆分可独立验收的任务、实现和根据结果调整计划。明确授权的子任务进入现有队列；DevRelay 负责工程操作与交付门禁。规划和实验结论可以保存在 Issues、任务文件与项目产物中。

```bash
devrelay --project /path/to/project next
devrelay --project /path/to/project start 12
devrelay --project /path/to/project prepare 12
# Agent 在 start 返回的 worktree 中调查、实现并修复。
devrelay --project /path/to/project check 12
devrelay --project /path/to/project submit 12
```

从主 checkout 调用工具，读取 JSON 返回值中的工作树、状态和证据路径。`start` 只领取并创建或恢复工作树；`prepare` 在已领取的工作树中执行配置的全部 setup 命令，不调用模型，每次显式调用都重跑并使旧验收、AI 审查与合并门禁记录失效，历史日志保留，完成后需重新 `check`。准备失败或中断保存现场与日志，修复原因后重试 `prepare`。配置了 setup 时，准备缺失、失败或配置已过期都会阻止验收与交付。`check` 不启动实现 Agent；失败后由外层 Agent 读取证据并修复，再调用 `check`。`submit` 默认交付 draft PR；如果项目明确启用了自动审查或合并，它仍会推进相应策略。

已有本地提交或未合并 PR 的任务可以重新 `prepare`，再 `check/submit`，继续使用原分支与 PR。对已有 PR 会回读仓库、状态、head 与 base；已关闭、已合并、正在合并或身份不符时拒绝准备，保留现场并转入交付恢复。环境配置改变后，应先把最新目标分支集成到任务分支。`submit` 在提交、推送、PR 发布和回读边界重新比较任务标题、正文、授权、源码、配置与 base；观察到变化即停止，保留已发生的提交、推送及已回读的 PR 身份，重新验收后恢复，不自动回滚。

外层 Agent 的会话、计划、实验预算及工作树编辑期间的独占由外层负责。DevRelay 的单 worker 锁只覆盖工具命令执行，不覆盖两次调用之间的编辑；不要同时为该仓库启动 `watch`。恢复时先查看 `status` 和实际工作树，再继续编辑或重试工具。`resume` 专用于可选的 DevRelay Codex 编排，不能恢复外层 Agent 的会话。

`doctor` 默认检查工程工具，并检查已配置的 AI 交付依赖；启用了 `auto_review` 或 `auto_merge` 时仍要求 Codex。使用可选编排或显式 `review` 前运行 `doctor --with-codex`。完整操作约定见 [Agent 工作流](docs/agent-workflow.md)。

### GitHub 标签与 Issue 模板

GitHub 项目首次 `init` 会使用已认证的 `gh` 补齐仓库标签，并生成 `.github/ISSUE_TEMPLATE/devrelay-{bug,task,experiment,plan}.md`。需要对该仓库创建标签的权限；本地模式仍完全离线。标签直接创建在 GitHub，模板写入本地，提交到仓库默认分支后才会出现在新建 Issue 页面。初始化不会替你提交或推送文件。

默认标签沿用 DexLab 的管理规则：

| 维度 | 标签 |
|---|---|
| 主要任务类型 | `bug`、`enhancement`、`documentation`、`experiment`、`infrastructure` |
| 总体计划 | `scope:plan`（只跟踪子任务，队列拒绝执行） |
| 相关范围 | `area:backend`、`area:benchmark`、`area:cloth`、`area:contact`、`area:evaluation`、`area:hardware` |
| 唯一优先级 | `priority:P0`（首要修复）、`priority:P1`（当前核心交付）、`priority:P2`（后续扩展） |
| 执行授权 | `auto:approved` |
| 阻塞 | `blocked`、`needs-input` |

一个 Issue 对应一个可观察、可审查结果。实现该结果所需的代码、配置、测试和文档放在一起；只有具备独立价值或验收标准才拆子项，不仅按文件、层或阶段拆分。维护者在授权前确认粒度，执行与 AI 审查提示词也遵守该规则；队列不自动判断自然语言任务是否应该拆分。

模板包含问题与证据、交付范围、验收与命令、唯一的 `Depends on: none` 声明、资源与恢复条件。Bug、工程任务、实验模板只预设类型标签；总体计划模板预设 `scope:plan` 并在正文写入 `Task kind: plan`，只跟踪子任务。**任何生成的模板都不自动给新 Issue 加 `auto:approved` 或配置的授权标签，也不自动设优先级。** 创建仓库标签本身不授权任何 Issue；维护者确认任务后另行设置一个优先级及授权。依赖、阻塞和验收仍需满足。

带 `scope:plan` 标签或正文独立行 `Task kind: plan` 的总体计划父任务，即使误加授权也会被队列拒绝；旧模板的 `## Child issues and delivery plan / 子任务与交付计划` 标题也会被识别。该限制同时用于 GitHub／本地任务、自动选取及显式指定 ID 的执行、恢复和提交授权检查。普通子任务的 `Parent: #N` 链接不影响资格；不要把父任务写成必须先完成的依赖，否则子任务会等待父任务。未使用这些标记的自定义计划须由维护者补标记；`init --sync-github` 保留已有模板，不会自动升级它们。

已有标签保留名称、颜色和描述，已有模板文件与其他表单不覆盖；保留的项目模板继续使用项目自己的默认标签，维护者应单独核对。可以自行添加项目领域标签。对于已经接入的项目或网络/权限错误后的补齐，运行：

```bash
devrelay --project /path/to/project init --sync-github
```

该命令使用已有配置，不覆盖配置、忽略规则或模板；仅补齐缺失项。若自定义了 `[queue]`，按配置补齐授权、优先级及阻塞标签，并在模板中说明；类型标签与队列控制标签重名时不预设该类型。调用中断后先回读标签再恢复，避免重复创建或覆盖；模板只发布完整文件，写入失败后可补齐缺失文件。已归档的必需标签或大小写与队列配置不一致的名称会报告问题，交由维护者恢复标签或调整配置。

## GitHub Issue 队列

Issue 须开放，带 `auto:approved` 和唯一的 `priority:P0/P1/P2`，没有 `blocked` / `needs-input`，且正文包含唯一依赖声明：

```text
Depends on: none
# 或者保留一行：
Depends on: #12, #27
```

依赖须已关闭，关联关闭它的 PR 已合并到目标分支，且合并提交位于最新目标分支祖先链。关联关闭的 Issue 按完整 GitHub URL 核对仓库和编号；跨仓库的同号 Issue 不计入依赖交付或开放 PR 去重。开放 PR（包括其他分支上明确 Refs 的 PR）和已有工作树会排除重复领取。`next` 返回选择与全部排除原因，不领取任务；它会读取 GitHub 并刷新目标分支。

```bash
devrelay next
devrelay start 12
devrelay prepare 12
devrelay status
devrelay check 12
devrelay submit 12
```

工程工具与可选编排共用本节的任务授权、依赖与验收规则。外层 Agent 的报告不代替 `check` 产生的有效证据。

`submit` 仅接受仍有效的验证证据，然后提交、推送并创建或更新 **draft PR**。`run --submit` 可串联这些步骤。GitHub 远程必须解析为唯一 fetch URL 和唯一 push URL，两者均须指向 `project.repository`；SSH 与 HTTPS 可混用。多个 fetch/push URL 会被拒绝，不会执行 Git 的 push 扇出。为了绑定实际端点，作用于该远程的 `insteadOf` / `pushInsteadOf` URL 改写也会被明确拒绝；请直接配置 GitHub URL。Git 配置会在实际传输的工作树中校验，因此 `includeIf` 的分支或路径条件不能绕过检查。提交和清理使用不能解析为远端名称的绝对路径传输参数，在同次 Git 调用中绑定已校验 URL；校验后新增的改写或 URL 同名远端别名都无法重定向或扇出，原有凭据和 CA 设置仍保留。提交直接保留配置远程的分支跟踪设置，兼容单分支克隆，并按现有 fetch refspec 更新跟踪引用，不假定其位于 `refs/remotes/<remote>/<branch>`；清理使用相同映射。默认停在 draft；可按下节开启自动审查与合并。PR 使用 `Refs #N`，不自动关闭整个 Issue，也不发布、部署或创建后台定时任务。

远程指向其他仓库时，错误会指出 fetch 或 push URL，并包含 `does not match project.repository`；修正该远程后再重试 GitHub 操作。

推送后的跟踪引用更新和跟踪引用清理使用首个匹配的正 fetch refspec。负 fetch refspec 仅限制抓取，不会取消这一跟踪映射。

传输保留 Git 对 `GIT_CONFIG_NOSYSTEM` 的布尔值规则：`0`/`false` 保留原有系统设置，`1`/`true` 禁用它们。其他 Git 布尔值写法同样支持；无效值会停止传输。

## 可选模式：Codex 程序编排

需要 DevRelay 启动实现 Agent 时，明确执行以下命令：

```bash
devrelay doctor --with-codex
devrelay run 12             # setup → Codex 实现 → 项目验收
devrelay run 12 --submit    # 再按项目策略交付
devrelay resume 12          # 恢复 DevRelay 保存的实现会话
```

`run` 每次只处理一个任务。无 ID 时优先恢复中断任务；已有失败或阻塞任务时返回 `needs_attention`，由维护者检查并明确恢复后再派发新任务。Codex 在 `.devrelay/worktrees/ID` 实现，DevRelay 执行验收。检查失败时，在配置的 `max_attempts` 范围内把失败信息交给相同 Codex 会话修复。Codex 报告外部阻塞时保存阻塞状态。不要用无 ID 的 `run` 或 `watch` 接管仍由外层 Agent 编辑的任务；切换前应先停止原执行者并检查现场。

### 持续自动领取 Issues

在已接入的 GitHub 项目中运行：

```bash
devrelay --project /path/to/project watch
# 调整空队列等待时间，或处理两条后退出：
devrelay --project /path/to/project watch --poll-interval 30 --max-tasks 2
```

`--interval` 是 `--poll-interval` 的简短别名，例如 `watch --interval 30`；两种写法都要求正整数，默认值均为 60 秒。

`--limit` 是 `--max-tasks` 的简短别名，例如 `watch --limit 2`；两种写法都要求正整数，省略时默认不限制任务数量。

`watch` 按配置的优先级和创建时间串行领取已授权、依赖已交付的 Issue，执行开发、验收、draft PR 和启用的独立只读审查。项目 `.devrelay/config.toml` 的 `[delivery] auto_merge` 明确选择交付模式；默认 `false`，CLI 不会替你开启授权：

- **人工合并**（`auto_merge = false`）：提交 draft PR（及启用的审查）成功后立即领取下一条，不等待合并，不将 draft 标为 ready，已提交任务不会重复开发。
- **验收通过后自动合并**（`auto_merge = true`）：强制包含独立审查；满足证据、CI、GitHub 审查与分支保护后请求准确 HEAD 合并，等待 GitHub 确认 `MERGED`，重跑合并后 setup/checks 并完成配置的清理，记录 `completed` 后才领取下一条。适用 CI、必需人工批准和保护条件尚未满足时，保留当前任务并按 `--poll-interval` 轮询；不会在等待期间领取其他 Issue。

队列为空时也按 `--poll-interval`（默认 60 秒）检查，新 Issue 获授权后即可被领取。原有授权、阻塞、依赖、重复领取和验收门禁全部保留。自动模式的配置示例见下节；仅修改配置不会启动执行器。

`watch` 是前台长运行命令，终端和机器需要保持运行，不会安装后台服务或开机启动。输出为实时刷新的 JSON Lines 事件及最终状态；`watch_started.mode` 为 `manual_merge` 或 `auto_merge`，`merge_waiting` 事件说明当前等待原因。`--max-tasks` 计算本次完成的交付（包括恢复的交付）：人工模式计成功 PR，自动模式计合并后验收及清理通过的任务；省略则持续等待。

运行期间一直持有单 worker 锁，包括空队列、CI 和合并队列等待时；`status` / `next` 仍可查看状态，其他 DevRelay 写操作需要先停止 `watch`。`Ctrl+C` 或 `SIGTERM` 退出 130 并保留现场。重新启动优先恢复中断的实现、提交和交付；自动模式回读已有 PR、复用仍有效的审查，恢复排队合并、合并后验收及清理。审查进程已成功且完整证据已保存时直接恢复结论；没有完整证据的审查停在 `review_blocked`，需显式 `review ID`，不会默默重复调用审查。人工模式的 PR 留给维护者合并。

验收失败、外部阻塞、审查拒绝、失败/跳过的 CI、未解决审查反馈、冲突、撤销授权、证据过期、网络/API 错误或配置变动会停止领取并保留工作树与日志。尚未出现/完成的 CI 和待满足的保护条件继续等待。检查 `devrelay status`，按 `resume` / `check` / `submit` / `review` / `merge` 明确恢复后，再启动 `watch`；`post_merge_failed` 和 `cleanup_failed` 不会自动重试。新任务从最新目标分支开始；依赖任务仍须关闭且交付关联的 PR 已合并。

## 自动审查与合并

在项目 `.devrelay/config.toml` 中配置，并在运行任务前纳入版本管理：

```toml
[delivery]
auto_review = true
auto_merge = true
required_checks = ["test", "build"] # 改成 GitHub 实际检查名称
allow_no_ci = false
cleanup_worktrees = true
cleanup_branches = true
```

两个自动开关默认均为 `false`。仅开启 `auto_review` 会审查并保留 draft；开启 `auto_merge` 会自动包含审查，通过门禁后标记 ready 并请求 squash 合并。`required_checks` 防止预期 CI 尚未出现就被忽略；所有已报告检查也必须成功。跳过、中立、失败或运行中的检查不算成功。没有 CI 时默认等待；确实没有 GitHub CI 的项目可明确设置 `allow_no_ci = true`，本地验收与审查仍须通过，指定的必需检查也不会豁免。

```bash
devrelay submit 12         # 已验收实现 → draft PR → 启用的审查/合并策略
devrelay review 12         # 显式启动一次新的只读 AI 审查
devrelay merge 12          # 开启 auto_merge 后，重读门禁并推进/恢复合并
devrelay resume 12 --submit
```

审查使用新的 Codex 会话，固定 `read-only`，不复用实现会话。结论、发现和日志保存本地；AI 审查不会替你提交 GitHub APPROVE，也不能代替分支保护要求的人工批准。审查拒绝、源码/提交/base/配置/Issue 要求变化、撤销任务授权、未解决的审查线程或 GitHub 阻塞条件均阻止合并。每次读取最多 100 个 CI、审查及线程；数据不完整或 API 出错会阻止推进。

合并通过 `--squash --match-head-commit SHA` 固定已审查的提交，不使用 `--admin` 或跳过保护。请求成功不等于已经合并：必须回读 `MERGED` 与合并提交；进入合并队列或 CI 未完成时保存 `merge_waiting`，下次执行 `merge` 或 `run --submit` 继续。单次 `merge` 命令有界运行；开启 `auto_merge` 的 `watch` 持续轮询，直到合并后验收完成。GitHub 合并队列决定实际合并策略，DevRelay 不绕过队列。

发送合并命令前持久化 `merge_request`（HEAD/base/源码/配置/任务证据）。排队时只回读 GitHub，不重复发送请求；超时或崩溃后也先回读。若 PR 尚未合并且远端队列/自动合并均不存在，保存阻塞状态；确认 GitHub 状态后显式 `devrelay merge ID --retry-request` 才允许重试。该选项仍要求当前验收、审查、授权及所有门禁通过，远端仍排队时只等待。

检测到撤销授权、配置/证据变化或排队后的阻塞反馈时，会尝试撤销已记录请求的 GitHub 自动合并/队列条目并回读结果，保留 `merge_cancellation`。撤销失败保存明确错误，需在 GitHub 移除待合并请求后恢复。`Ctrl+C`/`SIGTERM` 保留排队请求供重启回读；GitHub 队列可能在控制器停止期间继续执行。授权检查、撤销和远端合并之间存在竞态：若远端已合并，记录该事实并停下处理，不宣称能够撤销已发生的合并。

审查崩溃恢复只读取本任务直属、实际归属一致且无符号链接的运行目录与证据文件；进程回执必须匹配当前工作树、日志路径及配置的只读审查命令。证据缺失、身份不符或路径越界时保留现场并要求显式 `review ID`，不重用无关 approve、不在任务目录之外写入审查结果。

合并后刷新目标分支，核对合并提交的祖先关系与源码树，再在新的 detached 工作树执行全部 setup 和 checks，通过后记录 `completed`。API 超时会先回读远端，已合并时只恢复验收。合并后树不一致或验收失败保存 `post_merge_failed`，阻止无 ID 自动派发；保留失败工作树与证据。GitHub 的 HEAD 条件不原子锁定 base，因此合并前刷新与合并后树核对都需要；失败后合并已发生，需要维护者处理，DevRelay 不自动回滚。

若需要修改 PR，先在保留的任务工作树修复，执行 `check ID`、`submit ID`；新提交会使旧审查失效。配置改动也要求重新验收。Issue 的关闭与依赖交付关联仍由维护者管理；自动合并不扩大 Issue 的关闭范围。

### 合并后清理

`cleanup_worktrees` 和 `cleanup_branches` 默认均为 `false`，在运行任务前按项目开启。前者删除成功任务的实现 worktree；后者删除该任务的本地、远端及本地远端跟踪分支，需要同时开启 worktree 清理。仅开启 `cleanup_worktrees` 可以保留分支。

清理要求远端 PR 已合并、合并提交仍在目标分支中、合并后 setup/checks 已通过。验收失败保留任务分支、实现 worktree 和失败的验收 worktree；即使之后重试成功，旧的失败验收 worktree 也保留，并列在 `retained_worktrees`。成功验收的临时 detached worktree 仍按原行为自动回收。

清理只针对任务记录的路径和准确提交，不强制删除 worktree，不批量 prune。新的已跟踪/未忽略文件改动、锁定的 worktree、分支推进或被其他 worktree 使用都会停止清理。删除 worktree 会回收其中被忽略的环境、缓存和输出；需要长期保存的文件应放在工作树外。验证、审查、清理日志及任务状态保存在 Git common directory，始终保留。

清理错误单独记录为 `cleanup_failed`，保留成功验收的事实，并暂停无 ID 自动派发。已完成的清理步骤可能已经删除部分资源；修复权限、占用或新改动等原因后，执行 `devrelay merge ID`，它会读取实际 Git 状态，恢复清理，不重复合并或重跑仍有效的验收。远端分支已被 GitHub 删除也可正常恢复。远端删除绑定预期 SHA，本地引用删除也核对预期 SHA，分支变化时保留。若需要由 DevRelay 保证“验收后才删除”，应关闭 GitHub 仓库自身的合并后自动删除选项；DevRelay 无法延迟外部触发的删除。

## 本地试跑

本地任务无需 GitHub 登录，适合先验证项目接入：

```bash
devrelay init --provider local
devrelay task add fix-1 --title "修复一个明确问题" --file /path/to/task.md
devrelay start fix-1
devrelay prepare fix-1
# 外层 Agent 在返回的 worktree 中实现。
devrelay check fix-1
devrelay submit fix-1 --local
```

`task add` 明确授权该本地任务；缺少依赖行时添加 `Depends on: none`。本地任务也可声明 `Depends on: #fix-1`。`submit --local` 只创建本地验证提交；维护者将提交集成到配置的目标分支后，执行 `devrelay task complete fix-1`，依赖任务才可运行。完成要求当前有效验收、任务要求与配置一致，实际工作树和 HEAD 与交付提交及验收树一致，且该提交位于目标分支祖先链。新增内容即使重新验收，也须再次提交并集成后才能完成；失败不会解锁依赖。验收模板见 [任务示例](examples/task.md)。

## 验证与恢复

- 验证绑定源码树、HEAD、目标 base、项目配置和任务要求。源码哈希包括已跟踪修改与未忽略新文件，使用临时 Git 索引，保留原索引。
- 所有源码证据 Git 操作均禁用本地 replacement refs，检查实际提交对象，不修改用户的替换引用。NUL 分隔的文件路径按字节解析，支持文件系统可表示的非 UTF-8 和含回车的文件名。
- 对已检出的子模块递归检查已跟踪修改和未忽略的新文件；隔离索引比较会忽略 `assume-unchanged`、`skip-worktree` 与 `core.fileMode=false` 等掩码，并覆盖 `submodule.*.ignore`、`.gitmodules` ignore 和隐藏未跟踪文件的状态配置。任一子模块不干净都会使验收、提交及自动审查/合并门禁失败，且 DevRelay 不会提交、清理、重置或更新子模块。
- 子模块未跟踪文件检测只应用 `.gitignore`，不会读取本地 `info/exclude` 或 `core.excludesFile`。检测与隔离索引比较强制使用 `core.ignoreCase=false`，因此 `build/` 等规则不能隐藏 `BuIlD/undelivered.txt`，且不修改用户配置。已暂存内容使用只读索引比较，生成源码证据时不重建真实子模块索引的缓存树，也禁用可选的索引刷新写入。
- 子模块已跟踪的普通文件还会绕过 attributes 和 clean filters，将原始字节与 HEAD blob 比较，防止本地转换隐藏未交付内容。支持边界是工作树字节必须与提交一致；依赖换行规范化、编码转换或 smudge filters 才能匹配提交的子模块会阻止验收。
- 子模块已提交的符号链接必须保留符号链接类型，且 `readlink` 原始目标字节必须与 HEAD blob 一致。`core.symlinks=false` 将链接检出或替换成普通文件时会阻止验收；需要在支持符号链接的工作树中验证。
- 干净且已提交的子模块 HEAD 变更通过父仓库 gitlink 纳入源码树并可正常交付；提交时会显式暂存已检出的 gitlink，不受子模块 ignore 配置影响。未初始化的子模块路径必须不存在或为空，残留文件会阻止验收；若验收命令需要子模块内容，应由项目的 `[[setup]]` 显式初始化。
- 检查过程中改变代码、HEAD、base、配置或任务要求，或提交时证据已过期，均拒绝提交。目标 base 前进后，先将其集成到任务分支，再运行检查；DevRelay 不自动解决冲突或重置工作。
- 验证结果、命令日志、执行时间和 Codex JSON 事件保存在 Git common directory 下的 `devrelay/`，跨工作树共享 SQLite 状态和单 worker 锁。每次使用新的记录目录。
- DevRelay 在启动 Codex 前保存本次运行目录和事件日志位置，并在首次收到有效 `thread.started` 时立即保存该任务的会话 ID。`devrelay resume ID` 会在调用 Codex 前校验已保存 ID 的格式及跨任务归属，并核对上次日志，再用该明确 ID 继续同一会话，绝不使用 `--last`；空的启动前日志可安全重试，而不完整、无效或冲突的会话事件会阻止恢复。
- Ctrl+C 或控制器崩溃不会把恢复静默切换到新会话。超时或中断会先向整个命令进程组发送 SIGTERM，宽限期后仍有非僵尸成员则发送 SIGKILL 并有界等待。确认进程组退出后才清除 `active_process`；无法终止时保留标记并阻止新 worker。每次启动命令（包括同一 worker 内的自动重试）前也会检查旧标记；旧组仍存活或有界探测无法确认退出时拒绝启动，保留原标记。
- 会话归属包括已保存 ID、各任务运行目录中留存的实现事件（包括控制器崩溃后尚未保存到状态的 ID）以及审查回执和事件；验收或审查失效后也不会遗忘归属。实现恢复拒绝其他任务的会话，也拒绝本任务或其他任务的审查会话；证据越界或不可读取时阻止恢复。读取限于所属运行目录，例外仅为下述严格校验的历史回执。
- 已完成的历史任务还可引用 Git common directory 下 `devrelay/integration/<32 位小写十六进制>/review.json`。仅在路径无符号链接、回执与已保存审查完全相同、PR URL 与 HEAD 均匹配已完成任务时兼容读取，继续保留该审查会话的归属，不修改任务或原始证据。活动任务、身份不匹配、不可读回执、未知目录布局或符号链接仍会阻止恢复；不会读取任意路径。
- 归属提取保留所有已知会话，但不对其他任务做完整回合验收：明确识别为非会话消息的截断尾部不会阻止无关任务恢复，损坏或含义不明的会话事件仍会阻止恢复。恢复任务自身的日志仍完整校验，不会绕过不完整或冲突事件。
- Codex JSONL 事件只按 LF 换行分割，分块输出也会保留消息内的 Unicode 分隔符；stderr 诊断单独留存，不作为 JSON 事件解析。
- 新的实现运行在启动前记录 `events.jsonl.format.json`，恢复时仍严格解析 stdout JSONL。没有格式标记和独立 stderr 日志的旧日志可包含普通 stderr 诊断；恢复时忽略这些文字，但仍拒绝损坏的 JSON/会话事件和冲突 ID。旧日志若只有诊断且没有已保存会话，会阻止恢复而不是静默创建新会话。
- 项目验收命令是信任边界。不要把“命令退出 0”当作业务正确或独立审查。官方引擎、评分规则和发布条件由项目自己的约定控制。
- 实现会话位于开发沙箱，控制端在宿主环境执行全部配置验收。代码实现就绪但沙箱权限阻止验证（例如 `ps`、临时目录或文件名创建）时，执行提示要求 Codex 返回 `ready`，在摘要中如实保留诊断。这不代表验收成功，也不跳过任何检查：宿主验收仍须实际通过才能提交。真实代码缺陷必须修复；阻止实现的缺失资源仍应报告为阻塞。控制端不会重新解释或覆盖 `blocked` 结果。
- 每条验收命令结束后、运行下一条命令之前及最终收尾时，均重新检查源码、HEAD 和递归子模块洁净状态；合并后验收在 setup 完成后及逐条验收命令之间采用同样检查。子模块变脏会立即使验证失败，保留修改与命令证据，不允许后续命令恢复文件来掩盖变更。这是命令边界的快照检查，不会持续监控在同一条命令内部修改后又恢复的内容。

## 开发与验证

本仓库的 `.devrelay/config.toml` 已配置自维护：经授权的 GitHub Issues 在独立工作树中实现，运行完整测试并创建 draft PR，再进行独立只读 AI 审查。自动合并关闭；合并后的验收与清理可通过 `devrelay merge ID` 执行。

自维护的可选编排执行器和 AI 审查固定使用 `gpt-6.1-sol`，已用 Codex CLI `0.162.1` 验证。若旧 CLI 拒绝此模型，先升级 CLI；修改模型配置后，已有任务需要重新验收与审查。外层 Agent 的模型由其宿主控制。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

测试使用临时真实 Git 仓库与假的 Codex CLI，覆盖工作树隔离、失败修复、精确会话恢复、源码与 base 漂移、提交门禁、审查、CI 等待、squash 合并和合并后验收。PR 测试向临时本地 bare 仓库真实推送，以测试替身模拟 GitHub API、请求超时与恢复。它们不调用模型、不推送公开仓库；真实项目还需要自己的运行验收。

非 UTF-8 文件名端到端测试在支持的文件系统上运行；只有创建无效 UTF-8 名称明确返回 `EILSEQ` 时才报告平台 skip，不代表产品验收通过。Git tree/index 字节路径回归无需检出这些名称，始终运行；权限错误和产品错误仍会使测试失败。

实现说明见 [架构](docs/architecture.md)。Codex 接口依据 [OpenAI 官方非交互执行文档](https://developers.openai.com/codex/noninteractive)。
