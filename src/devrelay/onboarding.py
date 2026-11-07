"""Additive GitHub issue-management setup; never authorize or edit issues."""

import json
import os
from pathlib import Path
import tempfile

from .errors import RelayError
from .git import command, validate_remote
from .providers import PLAN_LABEL
from .runner import assert_config


LABELS = (
    ("bug", "d73a4a", "Something isn't working / 问题修复"),
    ("enhancement", "a2eeef", "New feature or improvement / 功能改进"),
    ("documentation", "0075ca", "Documentation improvements / 文档完善"),
    ("experiment", "5319e7", "A bounded experiment with independent acceptance / 有界实验"),
    ("infrastructure", "bfd4f2", "Runtime, queue or reproducibility infrastructure / 运行基础设施"),
    (PLAN_LABEL, "5319e7", "Tracking plan parent; never executable / 总体计划父任务，不允许执行"),
)
QUEUE_LABELS = {
    "auto:approved": ("0e8a16", "Authorized execution; prerequisites still required / 已授权但仍需满足依赖"),
    "priority:P0": ("b60205", "Evidence gaps, data safety or execution blockers / 首要修复"),
    "priority:P1": ("d93f0b", "Current core delivery / 当前核心任务"),
    "priority:P2": ("fbca04", "Extensions after core prerequisites / 后续扩展"),
    "blocked": ("b60205", "Concrete blocker; record a recovery condition / 执行阻塞"),
    "needs-input": ("d876e3", "External data or a design decision required / 需要外部输入"),
}


def label_definitions(config):
    labels = {name: {"name": name, "color": color, "description": description}
              for name, color, description in LABELS}
    queue = config.data["queue"]
    groups = (([queue["approval_label"]], "0e8a16", "Maintainer execution authorization / 维护者执行授权"),
              (queue["priorities"], "fbca04", "Execution priority, in configured order / 配置顺序优先级"),
              (queue["blockers"], "b60205", "Execution blocker; record recovery condition / 执行阻塞"))
    for names, color, description in groups:
        for name in names:
            color_value, description_value = QUEUE_LABELS.get(name, (color, description))
            labels.setdefault(name, {"name": name, "color": color_value, "description": description_value})
    return list(labels.values())


def _api(config, endpoint, *, body=None):
    argv = ["gh", "api", "--hostname", "github.com", "--method", "POST" if body else "GET",
            endpoint, "-H", "Accept: application/vnd.github+json"]
    if body:
        argv += ["--input", "-"]
    raw = command(argv, config.root, input=json.dumps(body, ensure_ascii=False) if body else None)
    try:
        return json.loads(raw)
    except ValueError as error:
        raise RelayError("GitHub returned invalid label data") from error


def _existing_labels(config):
    labels, page = {}, 1
    while True:
        items = _api(config, f"repos/{config.repo}/labels?per_page=100&page={page}")
        if (not isinstance(items, list) or len(items) > 100
                or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                       or not item["name"] for item in items)):
            raise RelayError("GitHub returned an incomplete or invalid label inventory")
        for item in items:
            key = item["name"].casefold()
            if key in labels:
                raise RelayError("GitHub label inventory changed during pagination; retry setup")
            labels[key] = item
        if len(items) < 100:
            return labels
        page += 1


def templates(config):
    queue = config.data["queue"]
    policy = f"""<!-- Maintainer triage / 维护者分诊:
One issue delivers one observable, reviewable result. Keep its required code, config, tests and docs together.
一个 Issue 对应一个可观察、可审查结果；实现该结果所需的代码、配置、测试和文档放在一起。
Split child issues only for independent value or acceptance criteria, not merely by file, layer or phase.
只有具备独立价值或验收标准才拆子项，不仅按文件、层或阶段拆分。
Priority is optional (default: the second configured priority, or the first if only one exists).
优先级可选，默认使用配置中的第二档；只有一档时使用该档。不填写 Depends on 表示无依赖。
Priorities / 优先级: {json.dumps(queue['priorities'], ensure_ascii=False)}
Authorization / 执行授权: {json.dumps(queue['approval_label'], ensure_ascii=False)}
Blockers / 阻塞标签: {json.dumps(queue['blockers'], ensure_ascii=False)}
Creating this issue does not authorize execution. Maintainers grant authorization separately.
创建 Issue 不表示允许自动执行；维护者确认范围与验收后另行授予授权。
Authorization never bypasses dependencies, blockers or acceptance.
授权不绕过依赖、阻塞或验收；缺少外部输入时记录明确恢复条件。
Tracking parents must not receive execution authorization; authorize bounded children instead.
总体计划只跟踪交付，不加执行授权；给可独立验收的子任务授权。
Close only after full acceptance; a partial PR does not complete the issue.
完整验收通过后才关闭 Issue，部分 PR 不表示整个任务完成。
-->
"""
    body = """
## Problem and evidence / 问题与证据

<!-- Include affected behavior/artifact, version and reproducible evidence. -->

## Deliverable and exclusions / 交付与范围外内容

## Acceptance and commands / 验收与命令

- [ ] Observable deliverable and exact validation commands / 可核验交付及具体检查命令
- [ ] Relevant regression tests and documentation / 相关回归测试与文档

## Dependencies / 依赖

<!-- Optional declaration: none, or #12, #27; omission means no dependencies.
Dependencies must be closed and linked to merged PRs integrated into the configured base. / 声明可省略，省略表示无依赖；
依赖须已关闭，并有关联 PR 已合并到配置的目标分支。 -->
Depends on: none

## Resources, blockers and recovery / 资源、阻塞与恢复条件

<!-- State required data/decisions and recovery conditions; never include credentials. -->
"""
    tracking = """
Task kind: plan

## Child issues and delivery plan / 子任务与交付计划

- [ ] Link independently deliverable child issues / 链接可独立验收的子任务

<!-- This is a tracking parent, not an executable task. Do not add execution
authorization. Keep Task kind: plan or scope:plan; the queue rejects this parent
even if authorized. / 此 Issue 只跟踪总体交付，不应添加执行授权标签；
保留 Task kind: plan 或 scope:plan，误加授权也会被队列拒绝。 -->
"""
    definitions = (("devrelay-bug.md", "DevRelay bug / 问题修复", "Reproduce and fix a bounded bug / 复现并修复明确问题", "bug"),
                   ("devrelay-task.md", "DevRelay task / 工程任务", "One observable engineering deliverable / 一个可核验工程交付", "enhancement"),
                   ("devrelay-experiment.md", "DevRelay experiment / 有界实验", "Evidence and independent acceptance / 实验证据与独立验收", "experiment"),
                   ("devrelay-plan.md", "DevRelay plan / 总体计划", "Track independent child deliveries / 跟踪独立子任务交付", PLAN_LABEL))
    result = {}
    controls = {name.casefold() for name in (queue["approval_label"], *queue["priorities"], *queue["blockers"])}
    for filename, name, about, label in definitions:
        # A type name reused as a queue control must never be pre-applied by a template.
        default_label = "" if label.casefold() in controls else label
        front = (f"---\nname: {json.dumps(name, ensure_ascii=False)}\n"
                 f"about: {json.dumps(about, ensure_ascii=False)}\ntitle: ''\n"
                 f"labels: {json.dumps(default_label)}\nassignees: ''\n---\n\n")
        result[filename] = front + policy + body + (tracking if filename == "devrelay-plan.md" else "")
    return result


def _template_directory(config):
    directory = config.root / ".github" / "ISSUE_TEMPLATE"
    for path in (directory.parent, directory):
        if path.is_symlink() or path.exists() and not path.is_dir():
            raise RelayError(f"Issue template directory is a symlink or not a directory; preserved: {path}")
    return directory


def _preserved_name(label, item, queue_names):
    if item.get("archived_at"):
        raise RelayError(f"Required label is archived; restore it before setup: {label['name']}")
    if label["name"] in queue_names and item["name"] != label["name"]:
        raise RelayError(f"Existing queue label spelling differs; align queue configuration with {item['name']!r}")
    return item["name"]


def setup_github(config):
    if config.provider != "github":
        raise RelayError("GitHub issue setup requires project.provider=github")
    assert_config(config)
    validate_remote(config)
    directory = _template_directory(config)
    definitions = label_definitions(config)
    queue = config.data["queue"]
    queue_names = {queue["approval_label"], *queue["priorities"], *queue["blockers"]}
    try:
        existing = _existing_labels(config)
        result = {"repository": config.repo, "labels": {"created": [], "preserved": []},
                  "templates": {"created": [], "preserved": []},
                  "notes": ["Commit templates to the repository default branch to publish them.",
                            "New issues are not automatically authorized; maintainers assign authorization and one priority."]}
        for label in definitions:
            assert_config(config)
            key = label["name"].casefold()
            if key in existing:
                result["labels"]["preserved"].append(_preserved_name(label, existing[key], queue_names))
                continue
            validate_remote(config)
            try:
                created = _api(config, f"repos/{config.repo}/labels", body=label)
                if (not isinstance(created, dict) or not isinstance(created.get("name"), str)
                        or created["name"].casefold() != key):
                    raise RelayError("GitHub label creation readback differs from the requested name")
            except RelayError:
                # A timeout or a concurrent creator may have already created it; never force-update it.
                refreshed = _existing_labels(config)
                if key not in refreshed or refreshed[key].get("archived_at"):
                    raise
                existing = refreshed
                result["labels"]["preserved"].append(_preserved_name(label, existing[key], queue_names))
                continue
            existing[key] = created
            result["labels"]["created"].append(created["name"])
        assert_config(config)
        directory = _template_directory(config)
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in templates(config).items():
            path = directory / name
            if path.exists() or path.is_symlink():
                result["templates"]["preserved"].append(str(path.relative_to(config.root)))
                continue
            # Publish only complete files, without replacing any existing file or symlink.
            with tempfile.TemporaryDirectory(prefix=".devrelay-template-", dir=directory) as temporary:
                draft = Path(temporary) / "template.md"
                draft.write_text(content)
                try:
                    os.link(draft, path)
                except FileExistsError:
                    result["templates"]["preserved"].append(str(path.relative_to(config.root)))
                else:
                    result["templates"]["created"].append(str(path.relative_to(config.root)))
        return result
    except (RelayError, OSError) as error:
        raise RelayError(f"GitHub issue setup incomplete; existing files and labels are preserved. "
                         f"Resolve the cause, then rerun devrelay init --sync-github: {error}") from error
