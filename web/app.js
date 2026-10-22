"use strict";

// All displayed request, model, document and audit text is inserted as text nodes.
// The selected demo actor is an explicit sample header, not authentication.
const byId = (id) => document.getElementById(id);
const fallbackActors = [
  { id: "alpha-member", role: "member", tenant: "alpha", label: "A 空间 · 成员" },
  { id: "alpha-owner", role: "owner", tenant: "alpha", label: "A 空间 · 所有者" },
  { id: "alpha-viewer", role: "viewer", tenant: "alpha", label: "A 空间 · 只读访客" },
  { id: "beta-owner", role: "owner", tenant: "beta", label: "B 空间 · 所有者" }
];
const app = { actorId: "alpha-member", actor: fallbackActors[0], actors: fallbackActors, mode: "offline", tools: [], documents: [], tasks: [], run: null, message: "", scenario: null, busy: false, view: "workspace", epoch: 0, controller: new AbortController(), approvalId: null };
const statusLabels = { completed: "已完成", approval_required: "等待批准", denied: "权限拒绝", failed: "执行失败", budget_exceeded: "达到预算限制", rejected: "已拒绝", expired: "审批已过期" };
const stepLabels = { plan: "计划", tool_call: "工具调用", tool_result: "工具结果", approval: "审批", denied: "拒绝", error: "错误" };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function replace(id, ...nodes) { byId(id).replaceChildren(...nodes); }
function asText(value) {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try { return JSON.stringify(value, null, 2); } catch { return "无法显示此数据"; }
}
function listOf(value) { return Array.isArray(value) ? value : []; }
function displayTime(value) {
  if (value === undefined || value === null) return "";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? asText(value) : new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "medium" }).format(date);
}
function actorRecord(value) {
  if (typeof value === "string") return fallbackActors.find((a) => a.id === value) || { id: value, role: value.split("-").at(-1), tenant: value.split("-")[0], label: value };
  if (!value || typeof value !== "object") return fallbackActors.find((a) => a.id === app.actorId) || fallbackActors[0];
  const id = value.id || value.actor_id || app.actorId;
  const known = fallbackActors.find((a) => a.id === id) || {};
  return { ...known, ...value, id, role: value.role || known.role || "—", tenant: value.tenant || value.tenant_id || known.tenant || "—", label: value.label || value.display_name || known.label || id };
}
function current(epoch) { return epoch === app.epoch; }
function showError(message) { const box = byId("global-error"); box.textContent = message; box.classList.toggle("hidden", !message); }
function setBusy(busy) {
  app.busy = busy;
  byId("send-button").disabled = busy;
  byId("send-label").textContent = busy ? "执行中…" : "开始执行";
  byId("run-content").setAttribute("aria-busy", String(busy));
  document.querySelectorAll(".scenario-card, .approval-actions button, #reset-button, #refresh-state, #refresh-audit").forEach((button) => { button.disabled = busy; });
  if (!busy) byId("reset-button").disabled = app.actor.role !== "owner";
}
async function api(path, options = {}, epoch = app.epoch) {
  const headers = { "X-Demo-Actor": app.actorId, "Accept": "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method: options.method || "GET", headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: app.controller.signal, cache: "no-store" });
  let data;
  try { data = await response.json(); } catch { throw new Error("服务没有返回有效的 JSON，请确认本地演示服务已正常启动。"); }
  if (!current(epoch)) throw new DOMException("身份已切换", "AbortError");
  if (!response.ok) { const error = new Error(data?.error?.message || `请求失败（HTTP ${response.status}）`); error.code = data?.error?.code; throw error; }
  return data;
}
function errorMessage(error) { return `${error.code ? `${error.code} · ` : ""}${error.message || "请求失败，请重试。"}`; }
function renderIdentity() {
  const select = byId("actor-select");
  select.replaceChildren(...app.actors.map((actor) => { const option = element("option", "", actor.label); option.value = actor.id; return option; }));
  select.value = app.actorId;
  byId("actor-role").textContent = `role: ${app.actor.role}`;
  byId("actor-tenant").textContent = `tenant: ${asText(app.actor.tenant)}`;
  const online = app.mode === "ollama";
  byId("mode-badge").textContent = online ? "本地 Ollama" : "离线演示";
  byId("mode-badge").classList.toggle("mode-live", online);
  const notice = byId("mode-notice").querySelector("p");
  notice.replaceChildren(element("strong", "", online ? "已配置本地 Ollama 模式" : "默认使用离线演示决策器"), element("span", "", online ? "仅连接本机配置的模型；工具仍受身份、审批和预算限制。模型输出不代表执行成功。" : "按固定规则选择工具，不代表真实大模型推理；知识、身份与任务均为合成数据。"));
  byId("reset-button").disabled = app.busy || app.actor.role !== "owner";
  byId("reset-button").title = app.actor.role === "owner" ? "重置当前空间" : "仅空间所有者可以重置";
}
function emptyNode(text) { return element("div", "list-empty", text); }
function renderKnowledge() {
  if (!app.documents.length) { replace("knowledge-list", emptyNode("当前空间暂无可展示的知识文档。")); return; }
  replace("knowledge-list", ...app.documents.map((document, index) => {
    const card = element("article", "document-card");
    card.append(element("div", "document-label", `SYNTHETIC DOCUMENT / ${String(index + 1).padStart(2, "0")}`), element("h2", "", document.title || document.name || "合成知识文档"), element("p", "", document.content || document.text || document.excerpt || document.summary || "通过工作区检索此文档，查看相关引用。"));
    card.append(element("div", "row-meta", document.id || document.document_id || ""));
    return card;
  }));
}
function renderTasks() {
  if (!app.tasks.length) { replace("tasks-list", emptyNode("当前空间还没有任务。试试检索规则，再批准一条准备材料任务。")); return; }
  replace("tasks-list", ...app.tasks.map((task) => {
    const row = element("article", "data-row");
    const body = element("div", "row-content");
    body.append(element("h2", "", task.title || task.name || "演示任务"));
    if (task.description || task.details) body.append(element("p", "", task.description || task.details));
    body.append(element("div", "row-meta", [task.id || task.task_id, displayTime(task.created_at), task.created_by || task.actor_id].filter(Boolean).join(" · ")));
    row.append(element("span", "row-marker", "✓"), body, element("span", "row-badge", task.status || "已创建"));
    return row;
  }));
}
function renderTrace(steps) {
  if (!listOf(steps).length) { replace("trace-list", element("li", "trace-empty", "执行后显示计划、工具调用和审批状态。")); return; }
  replace("trace-list", ...steps.map((step, index) => {
    const row = element("li", `trace-step ${["denied", "error"].includes(step.type) ? step.type : ""}`);
    row.append(element("span", "step-dot", index + 1), element("h3", "", step.title || stepLabels[step.type] || "执行步骤"));
    if (step.detail !== undefined) row.append(element("p", "", asText(step.detail)));
    return row;
  }));
}
function renderEmptyRun() {
  const block = element("div", "empty-state");
  block.append(element("span", "empty-icon", "↗"), element("h3", "", "一次请求，一条可追溯的执行链。"), element("p", "", "尝试查询报销规则，或创建一条需要审批的准备任务。实际步骤和引用会显示在这里。"));
  replace("run-content", block);
  byId("run-status").textContent = "等待输入";
  byId("run-status").className = "quiet-badge";
  renderTrace([]);
}
function renderRun(result) {
  app.run = result;
  app.approvalId = result.approval?.id || null;
  const body = element("div", "result-body");
  body.append(element("p", "request-label", "YOUR REQUEST"), element("p", "request-text", app.message), element("p", "answer-text", result.answer || "服务未返回文字说明，请查看执行轨迹。"));
  if (listOf(result.citations).length) {
    const citations = element("div", "citation-list");
    result.citations.forEach((citation) => { const card = element("article", "citation-card"); card.append(element("h3", "", `来源 · ${citation.title || citation.document_id || "知识文档"}`), element("p", "", citation.excerpt || "")); citations.append(card); });
    body.append(citations);
  }
  if (result.approval?.id && result.status === "approval_required") {
    const card = element("section", "approval-card");
    card.append(element("h3", "", "这一步，需要你批准"), element("p", "", "确认下面的工具和参数。批准后才会写入；拒绝不会创建任务。"), element("pre", "data-block", asText({ tool: result.approval.tool, arguments: result.approval.arguments })));
    if (result.approval.expires_in !== undefined) card.append(element("p", "", `服务端给出的审批有效期：${asText(result.approval.expires_in)} 秒。`));
    const actions = element("div", "approval-actions");
    const approve = element("button", "button primary", "批准并执行"); approve.type = "button"; approve.addEventListener("click", () => decide("approve", result.approval.id));
    const reject = element("button", "button secondary", "拒绝操作"); reject.type = "button"; reject.addEventListener("click", () => decide("reject", result.approval.id));
    actions.append(approve, reject); card.append(actions); body.append(card);
  }
  body.append(element("div", "answer-meta", `run: ${result.run_id || "—"} · mode: ${result.mode || app.mode}`));
  replace("run-content", body);
  const status = byId("run-status");
  status.textContent = statusLabels[result.status] || result.status || "已返回";
  status.className = `quiet-badge ${result.status === "completed" ? "complete" : result.status === "approval_required" ? "pending" : "failed"}`;
  renderTrace(result.steps);
}
function renderLoading() {
  const block = element("div", "loading-block"); block.append(element("span", "loading-dot"), element("span", "", "正在执行受控工具流程…")); replace("run-content", block);
  byId("run-status").textContent = "执行中";
  byId("run-status").className = "quiet-badge";
}
async function refreshState(epoch = app.epoch) {
  const data = await api("/api/state", {}, epoch);
  if (data.actor) app.actor = actorRecord(data.actor);
  app.mode = data.mode || app.mode;
  app.documents = listOf(data.documents);
  app.tasks = listOf(data.tasks);
  renderIdentity(); renderKnowledge(); renderTasks();
}
async function bootstrap() {
  const epoch = app.epoch;
  showError(""); setBusy(true);
  try {
    const data = await api("/api/bootstrap", {}, epoch);
    const actors = listOf(data.actors).map(actorRecord);
    app.actors = actors.length ? actors : fallbackActors;
    app.actor = actorRecord(data.actor || app.actorId);
    app.mode = data.mode || "offline"; app.tools = listOf(data.tools); app.documents = listOf(data.documents); app.tasks = listOf(data.tasks);
    renderIdentity(); renderKnowledge(); renderTasks();
  } catch (error) { if (error.name !== "AbortError" && current(epoch)) showError(`工作台暂时无法连接。${errorMessage(error)} 请确认已使用说明中的命令启动本地服务，再刷新页面。`); }
  finally { if (current(epoch)) setBusy(false); }
}
async function execute(event) {
  event.preventDefault();
  if (app.busy) return;
  const message = byId("message").value.trim();
  if (!message) { byId("message").focus(); return; }
  const epoch = app.epoch; app.message = message; app.approvalId = null; app.run = null; showError(""); setBusy(true); renderLoading(); renderTrace([]);
  try {
    const body = { message };
    if (app.scenario) body.scenario = app.scenario;
    const result = await api("/api/run", { method: "POST", body }, epoch);
    renderRun(result);
    await refreshState(epoch);
  } catch (error) {
    if (error.name !== "AbortError" && current(epoch)) { showError(errorMessage(error)); if (!app.run) { replace("run-content", element("p", "inline-error", "本次请求未完成。输入已保留，可以重试。")); byId("run-status").textContent = "请求失败"; byId("run-status").className = "quiet-badge failed"; } }
  } finally { if (current(epoch)) setBusy(false); }
}
async function decide(decision, approvalId) {
  if (app.busy || app.approvalId !== approvalId) return;
  const epoch = app.epoch; showError(""); setBusy(true);
  try {
    const result = await api(`/api/approvals/${encodeURIComponent(approvalId)}`, { method: "POST", body: { decision } }, epoch);
    renderRun(result); await refreshState(epoch);
  } catch (error) { if (error.name !== "AbortError" && current(epoch)) showError(errorMessage(error)); }
  finally { if (current(epoch)) setBusy(false); }
}
async function loadAudit() {
  if (app.busy) return;
  const epoch = app.epoch; showError(""); setBusy(true); replace("audit-list", emptyNode("正在读取可见范围内的记录…"));
  try {
    const data = await api("/api/audit", {}, epoch);
    const events = listOf(Array.isArray(data) ? data : data.events);
    if (!events.length) replace("audit-list", emptyNode("当前可见范围内没有审计记录。"));
    else replace("audit-list", ...events.slice().reverse().map((event) => {
      const row = element("article", "data-row"); const body = element("div", "row-content");
      body.append(element("h2", "", event.action || event.event || event.type || "执行记录"));
      const details = event.details || event.detail || event.metadata;
      if (details !== undefined) body.append(element("p", "", asText(details)));
      body.append(element("div", "row-meta", [displayTime(event.timestamp || event.created_at || event.at), event.actor_id || event.actor, event.id].filter(Boolean).map(asText).join(" · ")));
      row.append(element("span", "row-marker", "◎"), body);
      if (event.status) row.append(element("span", "row-badge", statusLabels[event.status] || event.status));
      return row;
    }));
  } catch (error) { if (error.name !== "AbortError" && current(epoch)) replace("audit-list", element("div", "no-access", errorMessage(error))); }
  finally { if (current(epoch)) setBusy(false); }
}
function changeView(view) {
  app.view = view;
  document.querySelectorAll(".view").forEach((node) => node.classList.toggle("hidden", node.id !== `view-${view}`));
  document.querySelectorAll("[data-view]").forEach((node) => { const active = node.dataset.view === view; node.classList.toggle("active", active); if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
  if (view === "audit") loadAudit();
}
async function switchActor(actorId) {
  if (actorId === app.actorId) return;
  app.controller.abort(); app.controller = new AbortController(); app.epoch += 1;
  app.actorId = actorId; app.actor = actorRecord(actorId); app.run = null; app.approvalId = null; app.message = ""; app.scenario = null; app.tasks = []; app.documents = []; app.busy = false;
  byId("message").value = ""; showError(""); renderEmptyRun(); renderIdentity(); renderKnowledge(); renderTasks(); replace("audit-list", emptyNode("身份已切换，请刷新读取当前范围。"));
  await bootstrap();
  if (app.view === "audit") loadAudit();
}
function scenario(name) {
  if (app.busy) return;
  const messages = { travel: "请检索当前空间的报销规则，给出相关来源。", prepare: "请创建一个准备报销材料的任务。", permission: "请创建一个准备报销材料的任务。", isolation: "请在任务查询中额外指定其他租户参数。" };
  const scenarios = { travel: "knowledge", prepare: "create_task", permission: "create_task", isolation: "cross_tenant" };
  const choose = name === "permission" ? "alpha-viewer" : name === "isolation" ? "beta-owner" : null;
  const fill = () => { changeView("workspace"); app.scenario = scenarios[name] || null; byId("message").value = messages[name] || ""; byId("message").focus(); byId("composer-hint").textContent = name === "permission" ? "已切换只读身份；发送后查看服务端策略结果。" : name === "isolation" ? "已切换 B 空间；服务端会拒绝额外租户参数。" : "问题已填入，点击开始执行。"; };
  if (choose && choose !== app.actorId) {
    const expectedEpoch = app.epoch + 1;
    switchActor(choose).then(() => { if (current(expectedEpoch) && app.actorId === choose) fill(); });
  } else fill();
}
byId("run-form").addEventListener("submit", execute);
byId("message").addEventListener("input", () => { app.scenario = null; byId("composer-hint").textContent = "Enter 换行 · Ctrl / ⌘ + Enter 发送"; });
byId("message").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); byId("run-form").requestSubmit(); } });
byId("actor-select").addEventListener("change", (event) => switchActor(event.target.value));
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => changeView(button.dataset.view)));
document.querySelectorAll("[data-scenario]").forEach((button) => button.addEventListener("click", () => scenario(button.dataset.scenario)));
byId("refresh-audit").addEventListener("click", loadAudit);
byId("refresh-state").addEventListener("click", async () => { if (app.busy) return; const epoch = app.epoch; showError(""); setBusy(true); try { await refreshState(epoch); } catch (error) { if (error.name !== "AbortError" && current(epoch)) showError(errorMessage(error)); } finally { if (current(epoch)) setBusy(false); } });
byId("reset-button").addEventListener("click", async () => {
  if (app.busy || app.actor.role !== "owner") return;
  if (!window.confirm("重置当前演示空间？这会恢复合成种子数据，其他空间不受影响。")) return;
  const epoch = app.epoch; showError(""); setBusy(true);
  try { await api("/api/reset", { method: "POST", body: {} }, epoch); app.run = null; app.approvalId = null; renderEmptyRun(); await refreshState(epoch); }
  catch (error) { if (error.name !== "AbortError" && current(epoch)) showError(errorMessage(error)); }
  finally { if (current(epoch)) { setBusy(false); if (app.view === "audit") loadAudit(); } }
});
bootstrap();
