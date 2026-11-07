"use strict";

const $ = (id) => document.getElementById(id);
const state = {token: "", projects: [], id: null, base: null, source: "", data: {}, explicit: {}, changes: {}, tab: "policy", preview: null, pending: false, backups: {}, registrationPlan: null, registrationInput: null, replaceId: null};
const tabs = {policy: "接单策略", execution: "执行交付", commands: "项目验收", project: "项目", source: "源码", status: "状态"};
const groups = {
  policy: [["queue", "Issue 接单"], ["discovery", "Agent 新发现"]],
  execution: [["codex", "执行器"], ["delivery", "审查与交付"]],
  project: [["project", "项目与基线"]]
};
// Defaults and supported keys come from the selected runtime, never this table.
const fields = {
  "queue.mode": ["接单方式", "select", "manual=逐条人工授权|autonomous=自动分诊后执行", "自动分诊只执行符合项目范围的任务。"],
  "queue.allowed_authors": ["允许的 Issue 作者", "list", "", "GitHub 登录名，以逗号或换行分隔；留空表示不限制作者。"],
  "queue.auto_kinds": ["自动处理的任务类型", "list", "", "bug、feature、maintenance，以逗号分隔。"],
  "queue.scope": ["自动处理范围", "text", "", ""],
  "queue.approval_label": ["人工授权标签", "string", "", "此标签授权单个任务；自动模式具有独立的项目级授权。"],
  "queue.max_dispatches": ["自动调度预算", "number", "", "分诊与开发各计一个阶段；保存配置不会重置已用预算。"],
  "queue.priorities": ["优先级（高 → 低）", "list", "", ""],
  "queue.blockers": ["阻塞标签", "list", "", "包含任一标签时暂停处理。"],
  "discovery.enabled": ["记录新问题", "bool", "", ""],
  "discovery.auto_triage": ["自动分诊", "bool", "", "开启后，新发现可先调查再执行；关闭后需要逐条授权。独立于人工 Issue 的接单模式。"],
  "discovery.auto_review": ["自动审查 PR", "bool", "", ""],
  "discovery.max_per_task": ["每任务发现上限", "number", "", ""],
  "codex.executable": ["Codex 可执行文件", "string", "", ""],
  "codex.model": ["模型", "optional", "", ""],
  "codex.profile": ["Profile", "optional", "", ""],
  "codex.sandbox": ["实现沙箱", "select", "read-only=只读|workspace-write=工作区可写|danger-full-access=完整访问", "独立审查使用只读会话。"],
  "codex.timeout_seconds": ["调用超时（秒）", "number", "", ""],
  "codex.max_attempts": ["实现调用预算", "number", "", "包含首次实现与后续返工。"],
  "delivery.auto_review": ["自动审查 PR", "bool", "", ""],
  "delivery.auto_merge": ["自动合并", "bool", "", "通过全部交付门禁后自动合并到目标分支。"],
  "delivery.required_checks": ["必需 CI 检查名称", "list", "", "填写项目真实的 CI 检查名称，以逗号或换行分隔。"],
  "delivery.allow_no_ci": ["允许没有 CI 的交付", "bool", "", "仅影响 CI 门禁，本地验收仍须通过。"],
  "delivery.post_merge_checks": ["合并后验收", "bool", "", ""],
  "delivery.cleanup_worktrees": ["完成后删除工作树", "bool", "", ""],
  "delivery.cleanup_branches": ["完成后删除分支", "bool", "", "需要同时开启工作树删除。"],
  "project.provider": ["任务来源", "select", "local=本地任务|github=GitHub Issues", ""],
  "project.repository": ["GitHub 仓库", "optional", "", "使用 owner/repo 格式。"],
  "project.base_branch": ["目标分支", "string", "", ""],
  "project.remote": ["Git 远端", "string", "", ""],
  "project.branch_prefix": ["任务分支前缀", "string", "", ""]
};

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
let helpId = 0;
function helpButton(title, text) {
  const wrapper = node("span", undefined, "info-help");
  const button = node("button", "i", "info-button"); button.type = "button";
  button.setAttribute("aria-label", `${title}说明`);
  const tooltip = node("span", text, "tooltip"); tooltip.id = `help-${++helpId}`;
  tooltip.setAttribute("role", "tooltip"); tooltip.hidden = true;
  button.setAttribute("aria-describedby", tooltip.id);
  let pinned = false, timer;
  const show = () => { clearTimeout(timer); tooltip.hidden = false; };
  const hide = () => { clearTimeout(timer); tooltip.hidden = true; pinned = false; };
  wrapper.addEventListener("pointerenter", show);
  wrapper.addEventListener("pointerleave", () => {
    if (!pinned && document.activeElement !== button) timer = setTimeout(hide, 120);
  });
  button.addEventListener("focus", show);
  button.addEventListener("blur", hide);
  button.addEventListener("click", () => { if (pinned) hide(); else { show(); pinned = true; } });
  wrapper.addEventListener("dismiss-help", hide);
  wrapper.append(button, tooltip);
  return wrapper;
}
function dismissHelp(except = null) {
  for (const help of document.querySelectorAll(".info-help")) if (!help.contains(except)) help.dispatchEvent(new Event("dismiss-help"));
}
document.addEventListener("keydown", (event) => { if (event.key === "Escape") dismissHelp(); });
document.addEventListener("click", (event) => dismissHelp(event.target));
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = !message;
}
function equal(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function dirty() { return state.base && (state.source !== state.base.source || Object.keys(state.changes).length > 0 || Boolean($("settings").querySelector(":invalid"))); }
function refreshButtons() {
  const changed = dirty();
  $("save-state").textContent = changed ? "未保存" : "已保存";
  $("save-state").classList.toggle("unsaved", Boolean(changed));
  $("preview").disabled = state.pending || !state.base || !changed;
  $("save").disabled = state.pending || !state.preview?.changed || state.preview?.activity.busy;
  $("register-confirm").disabled = state.pending || !state.registrationPlan;
}
function changed(key, value) {
  const [section, field] = key.split(".");
  const before = field ? state.data[section]?.[field] : state.data[section];
  if (equal(before, value) || (value === null && before === undefined)) delete state.changes[key];
  else state.changes[key] = value;
  state.preview = null;
  refreshButtons();
}
async function api(operation, payload = {}) {
  return request(`/api/projects/${state.id}/${operation}`, payload);
}
async function request(path, payload = {}) {
  const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-DevRelay-Token": state.token}, body: JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}
async function run(action) {
  if (state.pending) return;
  state.pending = true;
  const controls = [...document.querySelectorAll("button")].map((el) => [el, el.disabled]);
  controls.forEach(([el]) => { el.disabled = true; });
  $("workspace").inert = true; $("register-form").inert = true;
  try { await action(); } catch (error) { notice(error.message, true); }
  finally {
    controls.forEach(([el, disabled]) => { if (el.isConnected) el.disabled = disabled; });
    $("workspace").inert = false; $("register-form").inert = false; state.pending = false; refreshButtons();
    if (state.base && !$("settings").checkValidity()) $("settings").reportValidity();
  }
}
function draft() { return {revision: state.base.revision, source: state.source, changes: state.changes}; }
async function materialize() {
  if (!$("settings").checkValidity()) throw new Error("请先修正表单中的无效值。");
  const result = await api("preview", draft());
  state.source = result.source; state.data = result.data; state.explicit = result.explicit; state.changes = {};
  return result;
}
async function loadProject(id) {
  state.id = id; state.base = null; state.preview = null; state.changes = {};
  $("empty-projects").hidden = true; $("reload").hidden = false;
  $("locate-project").hidden = false;
  $("locate-project").textContent = state.projects.find((p) => p.id === id).registered ? "重新定位" : "登记项目";
  $("workspace").hidden = true; $("unavailable").hidden = true; $("activity").hidden = true; notice("");
  $("project-name").textContent = state.projects.find((p) => p.id === id).name;
  $("project-description").textContent = "读取中…";
  for (const button of $("projects").children) {
    button.classList.toggle("active", button.dataset.id === id);
    if (button.dataset.id === id) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  try {
    const result = await api("inspect");
    state.base = result; state.source = result.source; state.data = result.data; state.explicit = result.explicit;
    $("project-description").textContent = result.data.project?.repository || result.root;
    $("activity").replaceChildren(node("span", result.activity.busy ? "仓库占用中" : "仓库未占用"), helpButton("仓库状态", result.activity.busy ? "执行器或任务占用期间不可保存配置。" : "未检测到 DevRelay 执行锁；无法确认外层 Agent 是否仍在编辑。"));
    $("activity").classList.toggle("busy", result.activity.busy); $("activity").hidden = false;
    $("workspace").hidden = false;
    $("diagnostics").replaceChildren(); $("diagnostics").hidden = true;
    if (result.errors.length) {
      state.tab = "source";
      notice(`配置需要修正：${result.errors.join("；")}`, true);
    }
    render();
  } catch (error) {
    $("unavailable").hidden = false;
    $("unavailable").replaceChildren(node("p", error.message, "error"));
    $("project-description").textContent = "项目不可用";
  }
}
function fieldControl(path) {
  const [title, type, choices, help] = fields[path];
  const [section, key] = path.split(".");
  const value = Object.hasOwn(state.changes, path) ? state.changes[path] : state.data[section]?.[key];
  const wrapper = node("div", undefined, `field${type === "text" ? " wide" : ""}`);
  const label = node("label", title); label.htmlFor = path;
  const labelRow = node("div", undefined, "field-label"); labelRow.append(label);
  const control = node(type === "select" ? "select" : type === "text" ? "textarea" : "input"); control.id = path;
  if (type === "select") {
    for (const choice of choices.split("|")) { const [key, title] = choice.split("="); const option = node("option", title); option.value = key; control.append(option); }
  } else if (type === "bool") control.type = "checkbox";
  else if (type === "number") { control.type = "number"; control.min = "1"; control.step = "1"; control.required = true; }
  else if (type !== "text") control.type = "text";
  if (type === "bool") control.checked = Boolean(value);
  else control.value = type === "list" ? (value || []).join(", ") : value ?? "";
  if (type === "string" || type === "text") control.required = true;
  if (type === "list") control.placeholder = "逗号分隔";
  if (type === "optional" && section === "codex") control.placeholder = "默认";
  if (help) {
    const hint = helpButton(title, help); labelRow.append(hint);
    control.setAttribute("aria-describedby", hint.querySelector("[role=tooltip]").id);
  }
  if (type === "bool") {
    const row = node("div", undefined, "toggle-row"); row.append(labelRow, control); wrapper.append(row);
  } else wrapper.append(labelRow, control);
  control.addEventListener("input", () => {
    let next = control.value;
    if (type === "bool") next = control.checked;
    if (type === "number") next = Number(control.value);
    if (type === "list") next = control.value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
    if (type === "optional" && !next.trim()) next = null;
    changed(path, next);
  });
  return wrapper;
}
function commandPanel(section) {
  const title = section === "setup" ? "环境准备" : "项目验收";
  const panel = node("section", undefined, "panel");
  const heading = node("div", undefined, "panel-heading"); heading.append(node("h2", title), helpButton(title, section === "checks" ? "开始任务前至少需要一条验收命令；按列表顺序执行。" : "按列表顺序执行，参数不经 shell 解释。")); panel.append(heading);
  const entries = structuredClone(state.changes[section] ?? state.data[section] ?? []);
  entries.forEach((entry, index) => {
    const row = node("div", undefined, "command"); const grid = node("div", undefined, "fields"); row.append(grid);
    for (const [key, title, type] of [["id", "名称", "text"], ["timeout_seconds", "超时（秒）", "number"], ["argv", "命令参数（JSON 数组）", "textarea"]]) {
      const wrapper = node("div", undefined, `field ${key === "argv" ? "argv" : ""}`);
      const label = node("label", title); const input = node(type === "textarea" ? "textarea" : "input"); input.id = `${section}-${index}-${key}`; label.htmlFor = input.id;
      if (type !== "textarea") input.type = type;
      input.required = true; if (type === "number") { input.min = "1"; input.step = "1"; }
      input.value = key === "argv" ? JSON.stringify(entry.argv) : entry[key] ?? 600;
      wrapper.append(label, input); grid.append(wrapper);
      input.addEventListener("input", () => {
        input.setCustomValidity("");
        try {
          const value = key === "argv" ? JSON.parse(input.value) : type === "number" ? Number(input.value) : input.value;
          if (key === "argv" && (!Array.isArray(value) || !value.length || value.some((part) => typeof part !== "string" || !part))) throw new Error("请填写非空字符串数组，例如 [\"npm\", \"run\", \"check\"]");
          entries[index][key] = value; changed(section, entries);
        } catch (error) { input.setCustomValidity(error.message); state.preview = null; refreshButtons(); }
      });
    }
    const remove = node("button", "移除", "button secondary"); remove.type = "button"; remove.setAttribute("aria-label", `移除${title}命令 ${entry.id}`);
    remove.onclick = () => { entries.splice(index, 1); changed(section, entries); render(); }; grid.append(remove); panel.append(row);
  });
  const add = node("button", "+ 添加命令", "button secondary"); add.type = "button";
  add.onclick = () => { if (!$("settings").reportValidity()) return; entries.push({id: `${section}-${entries.length + 1}`, argv: ["npm", "run", "check"], timeout_seconds: 600}); changed(section, entries); render(); }; panel.append(add);
  return panel;
}
function renderStatus() {
  $("setup-github").hidden = state.base.data.project?.provider !== "github";
  $("task-list").replaceChildren();
  if (!state.base.tasks.length) $("task-list").append(node("p", "暂无任务", "empty"));
  for (const task of state.base.tasks) {
    const row = node("div", undefined, "task-row"); row.append(node("strong", `#${task.id} ${task.title || ""}`), node("span", task.status));
    if (task.pr_url && /^https:\/\/github\.com\//.test(task.pr_url)) { const link = node("a", "查看 PR"); link.href = task.pr_url; link.target = "_blank"; link.rel = "noreferrer"; row.append(link); }
    $("task-list").append(row);
  }
  $("locations").replaceChildren();
  for (const [title, value] of [["版本", `DevRelay ${state.base.runtime.version}`], ["目标分支", state.base.data.project?.base_branch || "—"], ["配置文件", state.base.config_path], ["Controller", state.base.root], ["Runtime", state.base.runtime.path], ["Runtime SHA-256", state.base.runtime.sha256], ["Python", state.base.runtime.python], ["项目清单", state.catalog], ["仓库占用状态", state.base.activity.reason], ["上次保存的备份", state.backups[state.id]]]) if (value) $("locations").append(node("dt", title), node("dd", value));
  const defaults = node("details", undefined, "defaults"); defaults.append(node("summary", "运行版本默认值"));
  const values = node("dl");
  for (const [section, entries] of Object.entries(state.base.defaults)) if (entries && !Array.isArray(entries)) {
    for (const [key, value] of Object.entries(entries)) values.append(node("dt", `${section}.${key}`), node("dd", JSON.stringify(value)));
  }
  defaults.append(values); $("locations").parentElement.querySelector(".defaults")?.remove(); $("locations").after(defaults);
}
function render() {
  $("tabs").replaceChildren();
  for (const [id, title] of Object.entries(tabs)) {
    const button = node("button", title, "tab"); button.type = "button"; button.setAttribute("aria-pressed", String(state.tab === id));
    button.disabled = state.base.errors.length > 0 && !["source", "status"].includes(id);
    button.onclick = () => run(async () => {
      if (id === state.tab) return;
      if ((id === "source" || state.tab === "source") && dirty()) await materialize();
      if (!$("settings").checkValidity()) return;
      state.tab = id; render();
    }); $("tabs").append(button);
  }
  $("settings").replaceChildren(); $("settings").hidden = ["source", "status"].includes(state.tab);
  $("source-panel").hidden = state.tab !== "source"; $("status-panel").hidden = state.tab !== "status";
  if (state.tab === "source") $("source").value = state.source;
  else if (state.tab === "status") renderStatus();
  else if (state.tab === "commands") for (const section of ["setup", "checks"]) $("settings").append(commandPanel(section));
  else for (const [section, title] of groups[state.tab]) {
    const panel = node("section", undefined, "panel"); const heading = node("div", undefined, "panel-heading"); heading.append(node("h2", title));
    if (section === "codex") heading.append(helpButton(title, "用于 DevRelay 内置 Codex 编排与独立审查。外层 Agent 使用自身的模型配置。"));
    panel.append(heading);
    const grid = node("div", undefined, "fields");
    for (const path of Object.keys(fields)) if (path.startsWith(section + ".") && state.base.supported.includes(path)) grid.append(fieldControl(path));
    panel.append(grid); $("settings").append(panel);
  }
  refreshButtons();
}
$("settings").addEventListener("submit", (event) => event.preventDefault());
$("source").addEventListener("input", () => { state.source = $("source").value; state.preview = null; refreshButtons(); });
$("reload").onclick = () => { if (!dirty() || confirm("重新加载会放弃尚未保存的编辑。继续吗？")) run(() => loadProject(state.id)); };
$("preview").onclick = () => run(async () => {
  if (!$("settings").checkValidity()) return;
  notice(""); state.preview = await api("preview", draft());
  const names = {setup: "环境准备", checks: "验收", review: "审查", delivery: "交付"};
  $("impact").hidden = !state.preview.affected_stages.length;
  $("impact").textContent = `需重新检查：${state.preview.affected_stages.map((key) => names[key]).join("、")}。`;
  const before = state.base.data, after = state.preview.data, consequences = [];
  const enabled = (section, key) => after[section]?.[key] && !before[section]?.[key];
  if (after.queue.mode === "autonomous" && before.queue?.mode !== "autonomous") consequences.push("未逐条授权的人工 Issue 也会进入自动分诊。");
  if (enabled("discovery", "auto_triage")) consequences.push("Agent 新发现的问题将自动分诊，符合范围后执行。");
  if (before.queue?.allowed_authors?.length && !after.queue.allowed_authors?.length) consequences.push("将允许所有 GitHub 作者的 Issue 进入接单检查。");
  if (enabled("delivery", "auto_merge")) consequences.push(`通过门禁的 PR 将自动合并到 ${after.project.base_branch}。`);
  if (enabled("delivery", "allow_no_ci")) consequences.push("缺少 CI 的 PR 也可通过 CI 门禁。");
  if (enabled("delivery", "cleanup_worktrees")) consequences.push("交付完成后将删除任务工作树。");
  if (enabled("delivery", "cleanup_branches")) consequences.push("交付完成后将删除任务分支。");
  if (after.codex.sandbox === "danger-full-access" && before.codex?.sandbox !== "danger-full-access") consequences.push("实现命令将获得完整系统访问权限。");
  $("consequences").replaceChildren(...consequences.map((message) => node("li", message)));
  $("consequences").hidden = !consequences.length;
  $("preview-busy").hidden = !state.preview.activity.busy;
  $("preview-busy").textContent = `仓库当前被占用，暂不可保存。${state.preview.activity.reason}`;
  $("preview-dialog").setAttribute("aria-describedby", ["save-effect", ...["impact", "consequences", "preview-busy"].filter((id) => !$(id).hidden)].join(" "));
  $("diff").replaceChildren();
  for (const line of (state.preview.diff || "配置没有变化。").split("\n")) $("diff").append(node("div", line || " ", line.startsWith("+") ? "added" : line.startsWith("-") ? "removed" : ""));
  $("dialog-error").hidden = true; $("preview-dialog").showModal();
});
$("cancel").onclick = () => $("preview-dialog").close();
$("save").onclick = () => run(async () => {
  try {
    const result = await api("save", {revision: state.preview.revision, source: state.preview.source, preview_sha256: state.preview.preview_sha256});
    if (result.backup) state.backups[state.id] = result.backup;
    $("preview-dialog").close(); await loadProject(state.id);
  } catch (error) { $("dialog-error").textContent = error.message; $("dialog-error").hidden = false; }
});
for (const operation of ["queue", "doctor"]) $(operation).onclick = () => run(async () => {
  const output = $("diagnostics"); output.hidden = false; output.replaceChildren(node("p", "查询中…", "muted"));
  try {
    const result = await api(operation); output.replaceChildren();
    if (operation === "queue") {
      for (const [key, title] of [["eligible", "可执行"], ["triage_candidates", "待自动分诊"], ["excluded", "未进入队列"]]) {
        const group = node("div", undefined, "diagnostic-group"); group.append(node("h3", `${title} · ${result[key].length}`));
        for (const item of result[key]) group.append(node("p", `#${item.id} ${item.title || (item.reasons || []).join("；")}`)); output.append(group);
      }
    } else {
      const group = node("div", undefined, "diagnostic-group"); group.append(node("h3", result.ready ? "环境自检通过" : "环境需要处理"));
      const labels = {git: "Git", base: "目标分支", acceptance: "验收命令", git_identity: "Git 提交身份", codex: "Codex", codex_login: "Codex 登录", gh: "GitHub CLI", github_login: "GitHub 登录", remote_matches_repository: "远端仓库", controller: "项目登记"};
      for (const item of result.checks) group.append(node("p", `${item.ok ? "✓" : "×"} ${labels[item.id] || item.id}${item.value ? ` · ${item.value}` : ""}`));
      output.append(group);
    }
  } catch (error) { output.replaceChildren(node("p", error.message, "error")); }
});
window.addEventListener("beforeunload", (event) => { if (dirty()) { event.preventDefault(); event.returnValue = ""; } });
async function loadSession() {
  const response = await fetch("/api/session");
  const session = await response.json();
  if (!response.ok) throw new Error(session.error || "请从终端显示的本机地址打开网页。");
  state.token = session.token; state.projects = session.projects; state.catalog = session.catalog;
  $("projects").replaceChildren();
  for (const project of session.projects) {
    const button = node("button", undefined, "project-button"); button.dataset.id = project.id;
    const icon = node("span", project.name.slice(0, 2).toUpperCase(), "project-icon"); icon.setAttribute("aria-hidden", "true");
    button.append(icon, node("span", project.name));
    button.onclick = () => { if (project.id !== state.id && (!dirty() || confirm("切换项目会放弃尚未保存的编辑。继续吗？"))) run(() => loadProject(project.id)); };
    $("projects").append(button);
  }
  $("add-project").hidden = !session.catalog || !session.projects.length;
  return session;
}
function openRegistration(project = null) {
  state.replaceId = project?.registered ? project.id : null;
  state.registrationPlan = null; state.registrationInput = null;
  $("register-form").reset();
  $("register-path").value = project?.path || "";
  $("register-name").value = project?.name || "";
  $("register-title").textContent = state.replaceId ? "重新定位项目" : "添加项目";
  $("register-confirm").textContent = state.replaceId ? "保存位置" : "添加项目";
  $("register-review").hidden = true; $("register-error").hidden = true;
  refreshButtons(); $("register-dialog").showModal(); $("register-path").focus();
}
$("path-help").append(helpButton("仓库路径", "填写运行 DevRelay 的这台电脑上的仓库路径，可使用 ~。选择子目录时会识别仓库根目录。"));
$("add-project").onclick = () => openRegistration();
$("first-project").onclick = () => openRegistration();
$("locate-project").onclick = () => openRegistration(state.projects.find((p) => p.id === state.id));
$("register-cancel").onclick = () => $("register-dialog").close();
$("register-dialog").addEventListener("cancel", (event) => { if (state.pending) event.preventDefault(); });
$("register-form").addEventListener("input", () => {
  state.registrationPlan = null; $("register-review").hidden = true; $("register-error").hidden = true; refreshButtons();
});
$("register-form").onsubmit = (event) => {
  event.preventDefault();
  if (!$("register-form").reportValidity()) return;
  run(async () => {
    state.registrationPlan = null; $("register-error").hidden = true; $("register-review").hidden = true;
    try {
      const input = {path: $("register-path").value, name: $("register-name").value, replace_id: state.replaceId};
      const plan = await request("/api/projects/probe", input);
      state.registrationPlan = plan; state.registrationInput = input;
      const details = $("register-details"); details.replaceChildren();
      for (const [label, value] of [["仓库", plan.repository || plan.name], ["位置", plan.root], ["目标分支", plan.base], ["配置", plan.existing_config ? "保留现有配置" : "创建项目配置"]]) details.append(node("dt", label), node("dd", value));
      $("register-source").textContent = plan.source;
      $("register-effect").textContent = `${plan.existing_config ? "保留配置" : "创建 .devrelay/config.toml"}${plan.ignore_additions.length ? "，补齐 .gitignore" : ""}，登记到本机项目清单。不会启动任务。`;
      const warnings = [];
      if (plan.relocating) warnings.push(`项目入口将从 ${plan.previous_controller} 切换到 ${plan.root}。`);
      if (!plan.checks.length) warnings.push("尚未配置验收命令，添加后请在“项目验收”中填写。");
      $("register-warning").textContent = warnings.join(" "); $("register-warning").hidden = !warnings.length;
      $("register-confirm").textContent = plan.relocating ? "确认新位置" : plan.already_added ? "打开项目" : "添加项目";
      $("register-review").hidden = false;
    } catch (error) { $("register-error").textContent = error.message; $("register-error").hidden = false; }
  });
};
$("register-confirm").onclick = () => run(async () => {
  if (dirty() && !confirm("切换项目会放弃尚未保存的编辑。继续吗？")) return;
  $("register-error").hidden = true;
  try {
    const result = await request("/api/projects/add", {...state.registrationInput, revision: state.registrationPlan.revision});
    await loadSession(); $("register-dialog").close();
    state.tab = result.needs_acceptance ? "commands" : "status";
    await loadProject(result.id);
    notice(result.needs_acceptance ? "项目已添加，请填写验收命令。" : "项目已登记，可以运行环境检查。");
  } catch (error) {
    state.registrationPlan = null;
    $("register-error").textContent = error.message; $("register-error").hidden = false;
  }
});
$("setup-github").onclick = () => run(async () => {
  if (dirty()) throw new Error("请先保存或放弃配置编辑，再初始化 GitHub。");
  if (!confirm("将在 GitHub 仓库补齐标签，并在本地创建缺失的 Issue 模板；保留已有内容，不授权或启动任务。继续吗？")) return;
  const result = await api("setup-github", {revision: state.base.revision});
  notice(`GitHub 已初始化：新增 ${result.labels.created.length} 个标签、${result.templates.created.length} 个模板。模板需通过项目 PR 提交。`);
});
run(async () => {
  const session = await loadSession();
  if (session.projects.length) await loadProject(session.projects[0].id);
  else {
    $("project-name").textContent = "项目"; $("project-description").textContent = "";
    $("reload").hidden = true; $("empty-projects").hidden = false;
  }
});
