import {api, ApiError, pathTo, streamRun} from "./api.js";
import {el, button, markdown, modeNames, statusBadge, statusNames} from "./render.js";

const $ = id => document.getElementById(id);
const terminal = run => ["succeeded", "failed", "cancelled", "interrupted"].includes(run.status);
const state = {
  conversations: [], cid: null, tree: null, view: null, workspace: null,
  pending: false, epoch: 0, stream: null, live: new Map(), form: null, restore: null,
};
let toastTimer;

function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 7000);
}
function selectedNode() { return state.tree?.nodes.find(node => node.id === state.view); }
function activeBranch() { return state.tree?.branches.find(branch => branch.id === state.tree.conversation.active_branch_id); }
function activeRun() { return state.tree?.runs.find(run => !terminal(run)); }
function project() { return Boolean(state.tree?.conversation.project_path); }
function base() { return `/conversations/${state.cid}`; }
function revision() { return state.tree.conversation.revision; }
function abortStream() { state.stream?.abort(); state.stream = null; }
function errorText(error) {
  if (error instanceof ApiError) return error.message;
  return "未能完成操作，请检查本地服务连接。已提交的请求不会自动重发；刷新后确认结果。";
}
function loginPage() {
  abortStream(); state.epoch++; state.cid = null; state.tree = null; state.live.clear();
  for (const id of ["form-dialog", "restore-dialog"]) if ($(id).open) $(id).close();
  state.form = null; state.restore = null; $("toast").hidden = true;
  $("app").hidden = true; $("login").hidden = false; $("token").value = "";
}
async function act(action) {
  if (state.pending) return;
  state.pending = true; renderControls();
  try { await action(); }
  catch (error) {
    if (error.status === 401) { loginPage(); $("login-error").textContent = error.message; }
    else {
      toast(errorText(error));
      // A failed response may follow a successful restore/commit. Reconcile, never replay a write.
      if (state.cid) await refresh().catch(() => {});
    }
  } finally { state.pending = false; renderControls(); }
}
function action(fn) { return () => act(fn); }

async function listConversations() {
  state.conversations = (await api("/conversations")).conversations;
  renderConversations();
}
function renderConversations() {
  const root = $("conversations"); root.replaceChildren();
  const query = $("search").value.toLocaleLowerCase();
  for (const conversation of state.conversations) {
    if (!(conversation.title + (conversation.project_path || "")).toLocaleLowerCase().includes(query)) continue;
    const item = button("", action(() => selectConversation(conversation.id)), "conversation");
    item.setAttribute("aria-current", String(state.cid === conversation.id));
    item.append(el("strong", "", conversation.title), el("small", "", conversation.project_path || "普通对话"));
    item.title = conversation.project_path || conversation.title;
    root.append(item);
  }
  if (!root.children.length) root.append(el("p", "hint", query ? "没有匹配的会话。" : "还没有会话，开始一个新话题。"));
}
async function selectConversation(cid) {
  abortStream(); state.epoch++; state.cid = cid; state.tree = null; state.workspace = null;
  state.live.clear();
  localStorage.setItem("fixloop.conversation", cid);
  $("app").classList.remove("show-sidebar");
  $("notice").hidden = true;
  await refresh();
  renderConversations();
  startFollowing();
}
async function refresh() {
  if (!state.cid) return;
  const cid = state.cid, epoch = state.epoch;
  const tree = await api(`/conversations/${cid}/tree`);
  if (cid !== state.cid || epoch !== state.epoch) return;
  state.tree = tree; state.view = tree.conversation.view_node_id;
  state.workspace = null;
  if (!tree.runs.some(run => !terminal(run))) {
    try {
      const workspace = await api(`/conversations/${cid}/workspace`);
      if (cid !== state.cid || epoch !== state.epoch) return;
      state.workspace = workspace;
    } catch (error) {
      if (error.status === 401) throw error;
      if (cid !== state.cid || epoch !== state.epoch) return;
      $("notice").textContent = "暂时无法检查项目文件状态。请检查文件或终端日志后刷新；恢复操作仍由服务核对。";
      $("notice").hidden = false;
    }
  }
  renderAll();
}
function renderAll() {
  const conversation = state.tree?.conversation;
  $("title").textContent = conversation?.title || "新的一轮，从一个问题开始";
  $("project-path").textContent = conversation?.project_path || "普通对话 · 无需项目";
  $("project-path").title = conversation?.project_path || "";
  renderMessages(); renderTree(); renderDetails(); renderRecovery(); renderControls();
}
function renderControls() {
  const busy = Boolean(activeRun()), blocked = Boolean(state.workspace?.pending?.length || state.workspace?.execution_blockers?.length);
  const settling = Boolean(state.stream && !busy);
  const locked = state.pending || busy || settling || blocked;
  $("send").disabled = locked || !$("prompt").value.trim();
  $("stop").hidden = !busy;
  $("stop").disabled = state.pending;
  $("mode").disabled = locked;
  for (const option of $("mode").options) option.disabled = option.value !== "chat" && !project();
  if (!project()) $("mode").value = "chat";
  $("prompt").disabled = locked;
  $("new-chat").disabled = state.pending;
  $("refresh").disabled = state.pending;
  $("rename").disabled = !state.tree || state.pending || settling || (project() && busy) || blocked;
  $("branches").disabled = !state.tree || locked;
  $("checkpoint").disabled = locked;
  document.querySelectorAll("[data-mutation]").forEach(node => { node.disabled = locked; });
  document.querySelectorAll("[data-view]").forEach(node => { node.disabled = state.pending || settling || (project() && busy) || blocked; });
  $("compose-hint").textContent = blocked ? "请先处理未完成的恢复" : busy ? "任务执行中，可停止" : settling ? "正在同步执行结果…" : "Enter 发送 · Shift + Enter 换行";
  const historical = state.tree && state.view !== activeBranch()?.head_node_id;
  const notice = $("history-notice"); notice.replaceChildren(); notice.hidden = !historical;
  if (historical) {
    notice.append(document.createTextNode("正在查看历史。发送消息仍接在当前执行分支末端。 "));
    const back = button("回到当前分支", action(() => viewNode(activeBranch().head_node_id)));
    back.disabled = state.pending || settling || (project() && busy) || blocked;
    const fork = button("从这里继续", action(() => forkAt(state.view)));
    fork.disabled = locked;
    notice.append(back, fork);
  }
}
function renderWelcome(root) {
  const welcome = el("div", "welcome");
  welcome.append(el("p", "eyebrow", "想法可以分叉，进展可以保留。"), el("h2", "", state.cid ? "这一轮，想聊些什么？" : "从一个问题，\n开始下一步。"));
  welcome.append(el("p", "", state.cid ? "直接提问，或在输入框中选择项目问答与修复。" : "写一段文字、讨论一个想法，或打开项目一起排查。每条思路都有自己的分支。"));
  if (!state.cid) {
    const options = el("div", "welcome-options");
    options.append(button("随便聊聊 →", () => createDialog(false), "secondary"), button("结合项目提问 ↗", () => createDialog(true), "secondary"));
    welcome.append(options);
  }
  root.append(welcome);
}
function renderMessages() {
  const root = $("messages"), previousTop = root.scrollTop;
  const follow = root.scrollHeight - root.scrollTop - root.clientHeight < 120;
  root.replaceChildren();
  const nodes = state.tree ? pathTo(state.tree.nodes, state.view) : [];
  if (!nodes.some(node => node.kind === "turn")) renderWelcome(root);
  for (const node of nodes) {
    if (node.kind !== "turn") continue;
    const run = state.tree.runs.find(item => item.node_id === node.id);
    const item = el("article", "message"); item.dataset.nodeId = node.id;
    const head = el("div", "message-head");
    head.append(el("strong", "", "你"), el("span", "pill", modeNames[node.mode]));
    item.append(head, el("div", "user-content", node.user_text));
    const actions = el("div", "message-actions");
    for (const [label, fn] of [["编辑并重新执行", () => editDialog(node)], ["重新生成", action(() => repeatNode(node))], ["从此处继续", action(() => forkAt(node.id))]]) {
      const control = button(label, fn); control.dataset.mutation = "true"; actions.append(control);
    }
    item.append(actions); root.append(item);
    if (!run) continue;
    const response = el("article", "message");
    const responseHead = el("div", "message-head");
    responseHead.append(el("strong", "", "FixLoop"), statusBadge(run.status)); response.append(responseHead);
    const live = state.live.get(run.id), answer = terminal(run) ? run.answer : live?.text || "";
    if (answer) response.append(markdown(answer));
    if (!terminal(run)) response.append(el("p", "progress", live?.progress || "正在准备这一轮…"));
    if (run.error) response.append(el("p", "error", `运行未完成：${friendlyError(run.error)}`));
    if (answer) response.append(button("复制回答", action(async () => { await navigator.clipboard.writeText(answer); toast("回答已复制"); })));
    root.append(response);
  }
  if (follow) root.scrollTop = root.scrollHeight;
  else root.scrollTop = previousTop;
}
function friendlyError(error) {
  return ({user: "用户停止", shutdown: "服务关闭", timeout: "执行超时", RuntimeError: "执行遇到错误，请检查终端", ValueError: "执行结果或输入未满足要求", OSError: "文件或存储操作失败"})[error] || error;
}
function renderTree() {
  const root = $("tree"), branches = $("branches"); root.replaceChildren(); branches.replaceChildren();
  const tree = state.tree;
  $("branch-count").textContent = `${tree?.branches.length || 0} 个分支`;
  $("branch-note").textContent = tree ? "● 执行位置　○ 历史节点　◇ 文件检查点" : "每条分支保留独立的后续对话。";
  if (!tree) return;
  for (const [index, branch] of tree.branches.entries()) {
    const option = el("option", "", `${index + 1} · ${branch.name}`); option.value = branch.id;
    option.selected = branch.id === tree.conversation.active_branch_id; branches.append(option);
  }
  const heads = new Set(tree.branches.map(branch => branch.head_node_id));
  function row(node, depth, count) {
    const id = node?.id || null;
    const label = !node ? "会话起点" : node.kind === "local_checkpoint" ? node.user_text || "文件检查点" : `${count} · ${node.user_text}`;
    const wrapper = el("div", `tree-row depth-${Math.min(depth, 6)}${node?.kind === "local_checkpoint" ? " checkpoint" : ""}${id === activeBranch().head_node_id ? " active" : ""}`);
    const control = button(label, action(() => viewNode(id)), ""); control.dataset.view = "true";
    control.title = label; control.setAttribute("aria-current", String(id === state.view)); wrapper.append(control);
    if (heads.has(id)) wrapper.append(el("span", "pill", id === activeBranch().head_node_id ? "执行" : "分支"));
    root.append(wrapper);
  }
  row(null, 0, 0);
  const children = new Map();
  for (const node of tree.nodes) {
    if (!children.has(node.parent_id)) children.set(node.parent_id, []);
    children.get(node.parent_id).push(node);
  }
  const stack = (children.get(null) || []).map(node => ({node, depth: 0, count: 0})).reverse();
  while (stack.length) {
    const {node, depth, count} = stack.pop();
    const next = count + (node.kind === "turn" ? 1 : 0);
    row(node, depth, next);
    for (const child of [...(children.get(node.id) || [])].reverse()) stack.push({node: child, depth: depth + 1, count: next});
  }
}
function renderDetails() {
  $("project-status").hidden = !project();
  $("manual-state").textContent = activeRun() ? "执行结束后检查文件状态" : state.workspace ? state.workspace.manual_changes ? "有本地修改，下一轮会自动保存检查点。" : "文件与当前分支状态一致" : "文件状态暂不可用";
  const root = $("run-detail"); root.replaceChildren();
  const node = selectedNode(), run = state.tree?.runs.find(item => item.node_id === node?.id);
  if (!run) { root.textContent = node?.kind === "local_checkpoint" ? "这是保存文件状态的检查点，不执行模型。" : "选择一轮对话查看结果。"; return; }
  function detail(label, value) { const line = el("div", "detail-line"); line.append(el("span", "", label), el("span", "", String(value))); root.append(line); }
  detail("状态", statusNames[run.status]); detail("模式", modeNames[node.mode]);
  const live = state.live.get(run.id);
  if (!terminal(run)) detail("进度", live?.progress || "等待进度");
  const report = run.artifacts?.verification || live?.report?.verification;
  if (report) { detail("验证", report.all_passed ? "通过" : "未通过"); detail("测试", `${report.passed} 通过 / ${report.total_tests} 总计`); }
  if (run.error) root.append(el("p", "error", friendlyError(run.error)));
  const files = run.artifacts?.changed_files;
  if (files) {
    detail("文件变化", `${files.length} 个路径`);
    const list = el("ul", "file-list");
    for (const path of files) list.append(el("li", "", path));
    root.append(list);
  }
  if (run.artifacts?.context?.summary_turns) detail("历史上下文", `已摘要 ${run.artifacts.context.summary_turns} 轮，完整记录保留`);
  const candidate = state.workspace?.candidates?.find(item => item.run_id === run.id);
  if (candidate) {
    detail("修复结果", {ready: "候选结果，尚未写入项目", applied: "已应用到项目", discarded: "已放弃候选"}[candidate.status]);
    if (candidate.status === "ready") {
      const preview = button("查看差异并应用", action(() => previewCandidate(run.id)), "secondary"); preview.dataset.mutation = "true";
      const discard = button("放弃候选", action(() => discardCandidate(run.id))); discard.dataset.mutation = "true";
      root.append(preview, discard);
    }
  }
}
function renderRecovery() {
  const root = $("recovery"); root.replaceChildren();
  const pending = state.workspace?.pending || [], blockers = state.workspace?.execution_blockers || [];
  root.hidden = !pending.length && !blockers.length;
  for (const resource of blockers) {
    root.append(el("p", "error", "尚未确认上次执行及其外部工具已停止。项目操作暂时锁定，文件不会自动恢复或覆盖。"));
    root.append(button("重新检查并停止遗留执行", action(async () => {
      const cid = resource.conversation_id || state.cid;
      await api(`/conversations/${cid}/runs/${resource.run_id}/reconcile`, {method: "POST", body: {}});
      await afterMutation();
    }), "secondary"));
  }
  for (const operation of pending) {
    root.append(el("p", "", "上次文件应用或恢复尚未结束，目录可能包含部分新文件和部分旧文件。完成恢复或回到恢复前状态后，才能继续执行。"));
    root.append(el("small", "", `状态：${operation.status} · 涉及 ${operation.changes.length} 个路径`));
    if (operation.recovery_decision) root.append(el("p", "", operation.recovery_decision === "rollback" ? "上次已选择回到恢复前，请继续完成该选择。" : "上次已选择完成恢复，请继续完成该选择。"));
    const complete = button("完成恢复", () => recoveryDialog(operation, "complete"), "secondary");
    const rollback = button("回到恢复前", () => recoveryDialog(operation, "rollback"), "secondary");
    complete.disabled = blockers.length > 0 || operation.recovery_decision === "rollback";
    rollback.disabled = blockers.length > 0 || operation.recovery_decision === "complete";
    root.append(complete, rollback);
  }
}

async function previewCandidate(runId) {
  const plan = await api(`${base()}/runs/${runId}/candidate-preview`, {method: "POST", body: {}});
  showPreview(plan, {kind: "candidate"});
}
function discardCandidate(runId) {
  formDialog("放弃候选结果", [], "确认放弃", async () => {
    await api(`${base()}/runs/${runId}/candidate-discard`, {method: "POST", body: {}});
    await afterMutation(); toast("候选已放弃，执行记录保留");
  });
}

async function viewNode(id) {
  await api(`${base()}/view`, {method: "POST", body: {node_id: id, expected_revision: revision()}});
  await refresh();
}
function formDialog(title, fields, submitLabel, submit) {
  state.form = submit;
  $("dialog-title").textContent = title; $("dialog-submit").textContent = submitLabel;
  $("dialog-error").textContent = "";
  const root = $("dialog-fields"); root.replaceChildren();
  for (const field of fields) {
    const wrapper = el("div", "dialog-field"), id = `field-${field.name}`;
    const label = el("label", "", field.label); label.htmlFor = id;
    const input = el(field.multiline ? "textarea" : "input");
    input.id = id; input.name = field.name; input.value = field.value || "";
    input.required = Boolean(field.required); input.placeholder = field.placeholder || "";
    input.maxLength = field.multiline ? 300000 : field.name === "project_path" ? 4096 : 200;
    wrapper.append(label, input);
    if (field.hint) wrapper.append(el("p", "hint", field.hint));
    root.append(wrapper);
  }
  $("form-dialog").showModal();
}
function createDialog(withProject = false) {
  if (state.pending) return;
  const fields = [{name: "title", label: "会话名称", placeholder: "新对话"}];
  fields.push({name: "project_path", label: "项目目录（可选）", required: withProject, placeholder: "C:/work/my-project", hint: "留空用于写作和日常问答；绑定项目后可阅读代码和执行修复。"});
  formDialog("新建会话", fields, "创建会话", async values => {
    const body = {title: values.title.trim() || "新对话"};
    if (values.project_path.trim()) body.project_path = values.project_path.trim();
    const conversation = await api("/conversations", {method: "POST", body});
    await listConversations(); await selectConversation(conversation.id);
    if (body.project_path) $("mode").value = "project_chat";
  });
}
function editDialog(node) {
  formDialog("编辑并重新执行", [{name: "text", label: "修改这一轮的请求", value: node.user_text, required: true, multiline: true}], project() ? "查看恢复预览" : "创建分支并执行", async values => {
    if (project()) await preview({action: "input", node_id: node.id}, {kind: "edit", node: node.id, text: values.text});
    else await repeatNode(node, values.text);
  });
}
async function repeatNode(node, text) {
  const kind = text === undefined ? "regenerate" : "edit";
  if (project()) { await preview({action: "input", node_id: node.id}, {kind, node: node.id, text}); return; }
  const body = {expected_revision: revision()}; if (text !== undefined) body.text = text;
  await api(`${base()}/nodes/${node.id}/${kind}`, {method: "POST", body});
  await afterMutation();
}
async function forkAt(id) {
  if (project()) await preview({action: "fork", node_id: id}, {kind: "fork"});
  else {
    await api(`${base()}/fork`, {method: "POST", body: {node_id: id, expected_revision: revision()}});
    await afterMutation(); toast("已创建分支，可继续发送消息");
  }
}
async function switchTo(id) {
  if (id === activeBranch().id) return;
  if (project()) await preview({action: "switch", branch_id: id}, {kind: "switch"});
  else { await api(`${base()}/switch`, {method: "POST", body: {branch_id: id, expected_revision: revision()}}); await afterMutation(); }
}
async function preview(body, intent) {
  const plan = await api(`${base()}/restores/preview`, {method: "POST", body: {...body, expected_revision: revision()}});
  showPreview(plan, intent);
}
function showPreview(plan, intent) {
  state.restore = {cid: state.cid, plan, intent};
  $("restore-title").textContent = intent.kind === "candidate" ? "候选修复：差异与应用预览" : ["edit", "regenerate"].includes(intent.kind) ? "恢复输入状态并重新执行" : intent.kind === "switch" ? "切换分支：文件恢复预览" : "从历史继续：文件恢复预览";
  $("restore-summary").textContent = `${plan.changes.length} 个路径将发生变化。请核对后确认。`;
  $("restore-manual").hidden = !plan.manual;
  const root = $("restore-changes"); root.replaceChildren();
  if (!plan.changes.length) root.append(el("p", "hint", "文件内容无需变化，将创建或选择对应会话分支。"));
  function describe(entry) {
    if (!entry) return "不存在";
    const type = entry.kind || entry.type || "文件";
    return `${type} · ${entry.size || 0} B${entry.digest ? ` · ${entry.digest.slice(0, 12)}` : ""}${entry.mode ? ` · 权限 ${entry.mode.toString(8)}` : ""}`;
  }
  for (const change of plan.changes) {
    const item = el("div", "change"); item.append(el("strong", "", change.path), el("small", "", `${describe(change.before)} → ${describe(change.after)}`)); root.append(item);
  }
  if (plan.diff) root.append(el("pre", "candidate-diff", plan.diff));
  $("restore-error").textContent = "";
  $("restore-confirm").textContent = intent.kind === "candidate" ? "应用到项目" : ["edit", "regenerate"].includes(intent.kind) ? "恢复并重新执行" : intent.kind === "switch" ? "恢复并切换" : "恢复并创建分支";
  $("restore-dialog").showModal();
}
function recoveryDialog(operation, decision) {
  formDialog(decision === "complete" ? "完成上次恢复" : "回到恢复前的状态", [], "确认处理", async () => {
    await api(`${base()}/restores/recover`, {method: "POST", body: {operation_id: operation.id, decision}});
    await afterMutation(); toast("恢复事务已处理");
  });
  $("dialog-fields").append(el("p", "", "服务会核对事务中的文件状态。未知外部修改会阻止覆盖；失败后请保留文件和终端日志，再处理冲突。"));
}
async function afterMutation() {
  await refresh(); await listConversations(); startFollowing();
}

function startFollowing() {
  const run = activeRun();
  if (!run || state.stream) return;
  const controller = new AbortController(); state.stream = controller;
  const cid = state.cid;
  const live = state.live.get(run.id) || {text: "", cursor: 0, progress: "准备执行", report: null};
  state.live.set(run.id, live);
  followRun(cid, run.id, controller, live).catch(error => {
    if (error.name === "AbortError") return;
    if (error.status === 401) loginPage();
    else toast(errorText(error));
  }).finally(() => {
    if (state.stream === controller) { state.stream = null; if (activeRun()) startFollowing(); renderControls(); }
  });
}
async function followRun(cid, rid, controller, live) {
  const path = `/conversations/${cid}/runs/${rid}`, signal = controller.signal;
  let failures = 0, finished = false;
  while (!signal.aborted && state.cid === cid && !finished) {
    try {
      await streamRun(path, live.cursor, signal, frame => {
        if (signal.aborted || state.cid !== cid) return;
        if (frame.event === "run.snapshot") {
          const index = state.tree.runs.findIndex(run => run.id === rid);
          if (index >= 0) state.tree.runs[index] = frame.data;
          finished = terminal(frame.data); renderMessages(); renderDetails(); renderControls();
          return;
        }
        const event = frame.data;
        if (event.run_id !== rid || !Number.isSafeInteger(event.sequence) || event.sequence <= live.cursor) return;
        live.cursor = event.sequence;
        if (frame.event === "answer.delta") live.text += event.data.text;
        else if (frame.event === "project.progress") live.progress = event.data.tool ? `正在执行 ${event.data.tool}` : `阶段：${event.data.stage}`;
        else if (frame.event === "project.report") live.report = event.data;
        else if (frame.event === "run.started") {
          const index = state.tree.runs.findIndex(run => run.id === rid);
          if (index >= 0) state.tree.runs[index] = event.data;
          live.progress = "正在执行";
        } else if (["run.stop_requested", "run.cancelling"].includes(frame.event)) live.progress = "正在停止执行并确认外部工具退出，项目文件保持不变…";
        renderMessages(); renderDetails(); renderControls();
      });
      if (finished) break;
      const snapshot = await api(path, {signal});
      if (terminal(snapshot.run)) { finished = true; break; }
      throw new Error("连接中断");
    } catch (error) {
      if (signal.aborted || error.status === 401) throw error;
      failures++;
      $("connection").textContent = `连接中断，正在重连（${failures}）`;
      live.progress = "连接中断，正在重连；后台任务继续执行";
      renderMessages();
      await new Promise(resolve => {
        const timer = setTimeout(done, Math.min(5000, 800 * failures));
        function done() { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); }
        signal.addEventListener("abort", done, {once: true});
      });
    }
  }
  if (finished && !signal.aborted && state.cid === cid) {
    $("connection").textContent = "已连接 · 本地";
    await refresh(); await listConversations();
  }
}

$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); const control = event.submitter; control.disabled = true;
  $("login-error").textContent = "";
  try {
    await api("/session", {method: "POST", body: {}, headers: {Authorization: `Bearer ${$("token").value.trim()}`}});
    $("token").value = ""; await boot();
  } catch (error) { $("login-error").textContent = errorText(error); }
  finally { control.disabled = false; }
});
$("logout").addEventListener("click", action(async () => {
  await api("/session", {method: "DELETE"}); loginPage();
}));
$("refresh").addEventListener("click", action(async () => { await refresh(); await listConversations(); startFollowing(); }));
$("new-chat").addEventListener("click", () => createDialog());
$("search").addEventListener("input", renderConversations);
$("rename").addEventListener("click", () => {
  formDialog("重命名会话", [{name: "title", label: "会话名称", value: state.tree.conversation.title, required: true}], "保存名称", async values => {
    await api(base(), {method: "PATCH", body: {title: values.title, expected_revision: revision()}}); await afterMutation();
  });
});
$("prompt").addEventListener("input", renderControls);
$("prompt").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); if (!$("send").disabled) $("composer").requestSubmit();
  }
});
$("composer").addEventListener("submit", event => {
  event.preventDefault();
  act(async () => {
    const text = $("prompt").value.trim(); if (!text) return;
    if (!state.cid) {
      const conversation = await api("/conversations", {method: "POST", body: {title: text.slice(0, 30)}});
      await listConversations(); await selectConversation(conversation.id);
    }
    await api(`${base()}/turns`, {method: "POST", body: {text, mode: $("mode").value, expected_revision: revision()}});
    $("prompt").value = ""; await afterMutation();
  });
});
$("stop").addEventListener("click", action(async () => {
  const run = activeRun(); if (!run) return;
  await api(`${base()}/runs/${run.id}/stop`, {method: "POST", body: {}});
  toast("已请求停止，正在等待执行退出并保存状态");
}));
$("branches").addEventListener("change", event => { const id = event.target.value; renderTree(); act(() => switchTo(id)); });
$("checkpoint").addEventListener("click", action(async () => {
  await api(`${base()}/checkpoints`, {method: "POST", body: {expected_revision: revision()}});
  await afterMutation(); toast("本地修改已保存");
}));
$("dialog-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.pending) return;
  const control = $("dialog-submit"); control.disabled = true;
  state.pending = true; renderControls();
  try {
    const values = Object.fromEntries(new FormData(event.target));
    await state.form(values); $("form-dialog").close();
  } catch (error) {
    if (error.status === 401) { $("form-dialog").close(); loginPage(); }
    else { $("dialog-error").textContent = errorText(error); if (state.cid) await refresh().catch(() => {}); }
  } finally { state.pending = false; control.disabled = false; renderControls(); }
});
$("restore-confirm").addEventListener("click", async () => {
  const pending = state.restore; if (!pending || state.pending) return;
  $("restore-confirm").disabled = true;
  state.pending = true; renderControls();
  try {
    if (pending.cid !== state.cid) throw new Error("会话已变化");
    const {intent, plan} = pending;
    if (["edit", "regenerate"].includes(intent.kind)) {
      const body = {plan_id: plan.id, expected_revision: plan.revision};
      if (intent.kind === "edit") body.text = intent.text;
      await api(`${base()}/nodes/${intent.node}/${intent.kind}`, {method: "POST", body});
    } else await api(`${base()}/restores/apply`, {method: "POST", body: {plan_id: plan.id}});
    $("restore-dialog").close(); state.restore = null;
    await afterMutation(); toast(intent.kind === "candidate" ? "候选修复已应用到项目" : "恢复已完成，原分支已保留");
  } catch (error) {
    // Never reuse an uncertain/expired plan automatically.
    $("restore-dialog").close(); state.restore = null;
    if (error.status === 401) loginPage();
    else { toast(error.status === 409 ? "恢复预览已失效或任务忙碌，请重新生成预览。" : errorText(error)); await refresh().catch(() => {}); }
  } finally { state.pending = false; $("restore-confirm").disabled = false; renderControls(); }
});
document.querySelectorAll("[data-close]").forEach(control => control.addEventListener("click", () => {
  if (state.pending) return;
  $(control.dataset.close).close();
  if (control.dataset.close === "restore-dialog") state.restore = null;
}));
for (const id of ["form-dialog", "restore-dialog"]) $(id).addEventListener("cancel", event => {
  if (state.pending) event.preventDefault();
  else if (id === "restore-dialog") state.restore = null;
});
$("toggle-sidebar").addEventListener("click", () => $("app").classList.toggle("show-sidebar"));
$("toggle-details").addEventListener("click", () => {
  if (window.matchMedia("(max-width: 960px)").matches) {
    $("app").classList.toggle("show-details");
    $("toggle-details").setAttribute("aria-expanded", String($("app").classList.contains("show-details")));
  } else {
    $("app").classList.toggle("hide-details");
    $("toggle-details").setAttribute("aria-expanded", String(!$("app").classList.contains("hide-details")));
  }
});
window.addEventListener("focus", () => {
  if (state.cid && !state.pending && !state.stream && !$("form-dialog").open && !$("restore-dialog").open) act(async () => { await refresh(); startFollowing(); });
});
async function boot() {
  await api("/health");
  $("login").hidden = true; $("app").hidden = false;
  $("connection").textContent = "已连接 · 本地";
  await listConversations();
  const stored = localStorage.getItem("fixloop.conversation");
  const cid = state.conversations.find(item => item.id === stored)?.id || state.conversations[0]?.id;
  if (cid) await selectConversation(cid); else renderAll();
}
boot().catch(error => {
  loginPage(); if (error.status !== 401) $("login-error").textContent = errorText(error);
});
