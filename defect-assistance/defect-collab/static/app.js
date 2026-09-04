"use strict";

const STATE_META = {
  NEW:        { label: "待确认", cls: "blue", lane: "main" },
  OPEN:       { label: "待处理", cls: "orange", lane: "main" },
  IN_PROGRESS:{ label: "处理中", cls: "purple", lane: "main" },
  FIXED:      { label: "待验证", cls: "yellow", lane: "main" },
  CLOSED:     { label: "已关闭", cls: "green", lane: "main" },
  DEFERRED:   { label: "已延期", cls: "gray", lane: "archive" },
  REJECTED:   { label: "已拒绝", cls: "red", lane: "archive" },
};

const LANE_TITLES = {
  main: "主流程",
  archive: "挂起 / 驳回（非活跃）",
};

const ROLE_HINTS = {
  tester: "可提交缺陷、验证修复、重开回归缺陷",
  product: "可确认并指派、调整处理节奏（延期/驳回/恢复）",
  developer: "可开始处理指派给自己的缺陷并提交修复",
};

const SEVERITY_OPTIONS = [
  ["S1", "S1 · 致命"], ["S2", "S2 · 严重"], ["S3", "S3 · 一般"], ["S4", "S4 · 轻微"],
];
const PRIORITY_OPTIONS = [
  ["P0", "P0 · 紧急"], ["P1", "P1 · 高"], ["P2", "P2 · 中"], ["P3", "P3 · 低"],
];

const $ = (sel) => document.querySelector(sel);

let users = [];
let bugs = [];
let currentUser = null;
let currentDetail = null;
let currentAction = null;
let actionFormVisible = false;

const els = {
  userSelect: $("#userSelect"),
  roleHint: $("#roleHint"),
  newBugBtn: $("#newBugBtn"),
  boardSummary: $("#boardSummary"),
  board: $("#board"),
  newBugOverlay: $("#newBugOverlay"),
  newBugForm: $("#newBugForm"),
  assigneeOptions: $("#newBugForm select[name='assignee_id']"),
  detailDrawer: $("#detailDrawer"),
  drawerBackdrop: $("#drawerBackdrop"),
  detailHead: $("#detailHead"),
  detailBody: $("#detailBody"),
  toast: $("#toast"),
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const res = await fetch(path, { ...options, headers });
  let data = {};
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

function toast(message, type = "info") {
  els.toast.textContent = message;
  els.toast.className = `toast ${type === "error" ? "error" : ""}`;
  els.toast.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { els.toast.hidden = true; }, 3200);
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function developerOptions(selected = "") {
  const devs = users.filter((u) => u.role === "developer");
  return devs.map((d) =>
    `<option value="${d.id}" ${String(selected) === String(d.id) ? "selected" : ""}>${esc(d.name)}（${esc(d.title)}）</option>`
  ).join("");
}

function renderUserPicker() {
  const groups = [
    ["tester", "测试"], ["product", "产品"], ["developer", "开发"],
  ];
  els.userSelect.innerHTML = groups.map(([role, label]) => `
    <optgroup label="${label}">
      ${users.filter((u) => u.role === role).map((u) =>
        `<option value="${u.id}">${esc(u.name)} · ${esc(u.title)}</option>`).join("")}
    </optgroup>`).join("");

  const saved = localStorage.getItem("defectflow-user");
  const target = (saved && users.find((u) => String(u.id) === saved))
    ? Number(saved) : users[0].id;
  els.userSelect.value = String(target);
  switchUser();
}

function switchUser() {
  const id = Number(els.userSelect.value);
  currentUser = users.find((u) => u.id === id);
  localStorage.setItem("defectflow-user", String(id));
  els.roleHint.textContent = `${currentUser.roleLabel} · ${ROLE_HINTS[currentUser.role]}`;
  els.newBugBtn.hidden = currentUser.role !== "tester";
  closeDetail();
  renderBoard();
}

function stateBadge(state) {
  const meta = STATE_META[state];
  return `<span class="state-badge state-${state}">${meta.label}</span>`;
}

function renderBoard() {
  const summaryParts = [];
  for (const state of ["NEW", "OPEN", "IN_PROGRESS", "FIXED", "CLOSED"]) {
    summaryParts.push(`${STATE_META[state].label} ${bugs.filter((b) => b.state === state).length}`);
  }
  els.boardSummary.textContent =
    `共 ${bugs.length} 个缺陷 · ${summaryParts.join(" · ")}`;

  const lanes = { main: [], archive: [] };
  for (const bug of bugs) lanes[STATE_META[bug.state].lane].push(bug);

  els.board.innerHTML = Object.entries(lanes).map(([lane, list]) => {
    const states = Object.keys(STATE_META).filter((s) => STATE_META[s].lane === lane);
    const columns = states.map((state) => {
      const group = list.filter((b) => b.state === state);
      const cards = group.length
        ? group.map(cardHtml).join("")
        : `<p class="muted" style="padding:4px 6px;font-size:12px">暂无缺陷</p>`;
      return `
        <section class="column" aria-label="${STATE_META[state].label}">
          <div class="column-head">
            <span class="state-dot dot-${state}"></span>
            <span>${STATE_META[state].label}</span>
            <span class="count">${group.length}</span>
          </div>
          ${cards}
        </section>`;
    }).join("");

    const title = `<div class="board-title">${LANE_TITLES[lane]}</div>`;
    return `${title}<div class="columns${lane === "archive" ? " is-archive" : ""}">${columns}</div>`;
  }).join("");

  document.querySelectorAll(".bug-card").forEach((card) => {
    card.addEventListener("click", () => openDetail(Number(card.dataset.id)));
  });
}

function cardHtml(bug) {
  const sev = SEVERITY_OPTIONS.find(([v]) => v === bug.severity);
  const pri = bug.priority ? PRIORITY_OPTIONS.find(([v]) => v === bug.priority) : null;
  const badge = (label) => `<span class="badge">${esc(label)}</span>`;
  return `
    <article class="bug-card" data-id="${bug.id}" tabindex="0" role="button"
             aria-label="查看缺陷 ${esc(bug.code)}">
      <div class="card-top">
        <span class="code">${esc(bug.code)}</span>
        ${pri ? `<span class="badge pri-${bug.priority}">${esc(pri[1])}</span>` : ""}
      </div>
      <div class="card-title">${esc(bug.title)}</div>
      <div class="badges">
        ${sev ? `<span class="badge sev-${bug.severity}">${esc(sev[1])}</span>` : ""}
        ${bug.module ? `<span class="badge">${esc(bug.module)}</span>` : ""}
      </div>
      <div class="card-meta">
        <span><b>${bug.assignee ? esc(bug.assignee.name) : "待指派"}</b> 负责</span>
        <span>${bug.eventCount || 0} 条动态</span>
      </div>
    </article>`;
}

// ------------------------------------------------------------------ 新建缺陷

function openNewBugModal() {
  if (!currentUser || currentUser.role !== "tester") return;
  els.assigneeOptions.innerHTML =
    `<option value="">先不指派，由产品在确认时分配</option>` + developerOptions();
  els.newBugForm.reset();
  els.newBugOverlay.hidden = false;
}

async function submitNewBug(ev) {
  ev.preventDefault();
  const fd = new FormData(els.newBugForm);
  const payload = {
    user_id: currentUser.id,
    title: fd.get("title"),
    severity: fd.get("severity"),
    module: fd.get("module"),
    description: fd.get("description"),
    assignee_id: fd.get("assignee_id") ? Number(fd.get("assignee_id")) : null,
  };
  const btn = els.newBugForm.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const detail = await api("/api/bugs", { method: "POST", body: JSON.stringify(payload) });
    els.newBugOverlay.hidden = true;
    toast("缺陷已提交，等待产品经理确认");
    await reloadBugs();
    openDetail(detail.id);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ 详情抽屉

async function openDetail(id) {
  if (!currentUser) return;
  try {
    currentDetail = await api(`/api/bugs/${id}?as=${currentUser.id}`);
    renderDetail(currentDetail);
    els.detailDrawer.hidden = false;
    els.drawerBackdrop.hidden = false;
  } catch (err) {
    toast(err.message, "error");
  }
}

function closeDetail() {
  currentDetail = null;
  currentAction = null;
  actionFormVisible = false;
  els.detailDrawer.hidden = true;
  els.drawerBackdrop.hidden = true;
}

function renderDetail(detail) {
  const bug = detail;
  const pri = bug.priority ? PRIORITY_OPTIONS.find(([v]) => v === bug.priority) : null;
  const sev = SEVERITY_OPTIONS.find(([v]) => v === bug.severity);

  els.detailHead.innerHTML = `
    <button type="button" class="drawer-close" id="drawerCloseBtn" aria-label="关闭详情">×</button>
    <div class="drawer-kicker">
      <span class="code">${esc(bug.code)}</span>
      ${stateBadge(bug.state)}
      ${pri ? `<span class="badge pri-${bug.priority}">${esc(pri[1])}</span>` : ""}
    </div>
    <h2 class="drawer-title">${esc(bug.title)}</h2>`;
  $("#drawerCloseBtn").addEventListener("click", closeDetail);

  const meta = (label, value) => `
    <div class="meta-item"><span>${label}</span><b>${value}</b></div>`;
  const actionsHtml = renderActions(detail);
  const timelineHtml = detail.timeline.length
    ? `<ul class="timeline">${detail.timeline.map(timelineItemHtml).join("")}</ul>`
    : `<p class="muted">暂无操作记录</p>`;

  els.detailBody.innerHTML = `
    <section class="block">
      <h3>基本信息</h3>
      <div class="meta-grid">
        ${meta("报告人", esc(bug.reporter?.name || "-"))}
        ${meta("指派人", bug.assignee ? esc(bug.assignee.name) + "（" + esc(bug.assignee.title) + "）" : "待指派")}
        ${meta("严重程度", sev ? esc(sev[1]) : bug.severity)}
        ${meta("所属模块", esc(bug.module || "-"))}
        ${meta("创建时间", fmtTime(bug.createdAt))}
        ${meta("最近更新", fmtTime(bug.updatedAt))}
      </div>
    </section>
    <section class="block">
      <h3>缺陷描述</h3>
      ${bug.description ? `<p class="desc">${esc(bug.description)}</p>` : `<p class="muted empty-desc">未填写补充说明</p>`}
    </section>
    <section class="block">
      <h3>可执行操作</h3>
      ${actionsHtml}
    </section>
    <section class="block">
      <h3>流转记录</h3>
      ${timelineHtml}
    </section>`;

  els.detailBody.querySelectorAll(".action-btn").forEach((btn) => {
    btn.addEventListener("click", () => onActionClick(btn.dataset.action));
  });
  const commentInput = $("#actionComment");
  if (commentInput) {
    commentInput.addEventListener("input", () => {
      commentInput.style.borderColor = commentInput.value.trim() ? "#d1d5db" : "";
    });
  }
  const cancelBtn = $("#cancelActionBtn");
  if (cancelBtn) cancelBtn.addEventListener("click", () => {
    currentAction = null;
    actionFormVisible = false;
    renderDetail(bug);
  });
  const submitBtn = $("#doActionBtn");
  if (submitBtn) submitBtn.addEventListener("click", () => executeAction(submitBtn.dataset.action));
}

function renderActions(detail) {
  const actions = detail.allowedActions || [];
  const actionBox = document.createElement("div");

  if (!actions.length) {
    actionBox.innerHTML = `<p class="no-action">当前状态没有你可执行的操作</p>`;
    return actionBox.innerHTML;
  }

  const chips = actions.map((a, i) =>
    `<button type="button" class="btn action-btn${i === 0 ? " btn-primary" : ""}" data-action="${a.action}">${esc(a.label)}</button>`
  ).join("");

  let form = "";
  const chosen = actions.find((a) => a.action === currentAction);
  if (actionFormVisible && chosen) {
    const commentHint = chosen.commentRequired
      ? `必填：${chosen.label}需要说明理由`
      : "可选：给协作方留言";
    const triageFields = chosen.fields.includes("priority")
      ? `
        <div class="form-row">
          <select id="actionPriority" aria-label="优先级">
            ${PRIORITY_OPTIONS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}
          </select>
          <select id="actionAssignee" aria-label="指派开发">
            <option value="">请选择开发人员 *</option>
            ${developerOptions(detail.assignee?.id)}
          </select>
        </div>`
      : "";
    form = `
      <div class="action-form">
        ${triageFields}
        <textarea id="actionComment" rows="2"
          placeholder="${commentHint}"></textarea>
        <div class="mini-actions">
          <button type="button" class="btn" id="cancelActionBtn">取消</button>
          <button type="button" class="btn btn-primary" id="doActionBtn"
                  data-action="${currentAction}">
            执行：${esc(chosen.label)}
          </button>
        </div>
      </div>`;
  }
  actionBox.innerHTML = `<div class="action-box"><h4>根据当前身份与缺陷状态，你可以：</h4>
    <div class="action-list">${chips}</div>${form}</div>`;
  return actionBox.innerHTML;
}

function onActionClick(action) {
  const detail = currentDetail;
  const item = detail.allowedActions.find((a) => a.action === action);
  if (!item) return;
  if (!item.commentRequired && item.fields.length === 0) {
    doExecuteAction(action, null);
    return;
  }
  currentAction = action;
  actionFormVisible = true;
  renderDetail(detail);
  const commentInput = $("#actionComment");
  if (commentInput) {
    if (item.commentRequired) commentInput.placeholder = "请填写处理说明（必填）";
  }
}

async function executeAction(action) {
  const commentEl = $("#actionComment");
  const payload = { user_id: currentUser.id, action, comment: commentEl ? commentEl.value.trim() : "" };
  if (action === "triage") {
    payload.priority = $("#actionPriority").value;
    payload.assignee_id = Number($("#actionAssignee").value);
  }
  await doExecuteAction(action, payload);
}

async function doExecuteAction(action, payload) {
  const detail = currentDetail;
  const btn = document.querySelector(`#doActionBtn, [data-action="${action}"].action-btn.btn-primary`);
  if (btn) btn.disabled = true;
  try {
    const next = await api(`/api/bugs/${detail.id}/actions`, {
      method: "POST",
      body: JSON.stringify(payload || { user_id: currentUser.id, action, comment: "" }),
    });
    currentDetail = next;
    currentAction = null;
    actionFormVisible = false;
    toast(`${ACTION_LABEL(action)}：已流转到「${STATE_META[next.state].label}」`);
    await reloadBugs();
    renderDetail(next);
  } catch (err) {
    toast(err.message, "error");
    if (btn) btn.disabled = false;
  }
}

function ACTION_LABEL(action) {
  const map = {
    triage: "确认并指派", start: "开始处理", fix: "提交修复",
    verify_pass: "验证通过", verify_fail: "验证不通过",
    defer: "延期处理", activate: "恢复处理", reject: "驳回", reopen: "重开缺陷",
  };
  return map[action] || action;
}

function timelineItemHtml(ev) {
  const cls = ev.action === "verify_pass" || ev.action === "create" ? "done" : "gray";
  const actor = ev.actor ? esc(ev.actor.name) : "系统";
  const flow = [ev.fromStateLabel, ev.toStateLabel]
    .filter(Boolean).map((s) => `<span class="state-badge state-${keyOfLabel(s)}" style="margin:0 2px">${esc(s)}</span>`)
    .join("<span style='color:#9ca3af'> → </span>");
  return `
    <li class="tl-item ${cls}">
      <span class="tl-dot"></span>
      <div class="tl-head"><b>${actor}</b> ${esc(ev.actionLabel)}</div>
      ${flow ? `<div class="tl-flow">${flow}</div>` : ""}
      ${ev.comment ? `<p class="tl-comment">${esc(ev.comment)}</p>` : ""}
      <div class="tl-time">${fmtTime(ev.createdAt)}</div>
    </li>`;
}

function keyOfLabel(label) {
  const found = Object.entries(STATE_META).find(([, v]) => v.label === label);
  return found ? found[0] : "";
}

async function reloadBugs() {
  bugs = (await api("/api/bugs")).items;
  renderBoard();
}

function init() {
  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => { $(`#${btn.dataset.close}`).hidden = true; });
  });
  els.userSelect.addEventListener("change", switchUser);
  els.newBugBtn.addEventListener("click", openNewBugModal);
  els.newBugForm.addEventListener("submit", submitNewBug);
  els.drawerBackdrop.addEventListener("click", closeDetail);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      closeDetail();
      els.newBugOverlay.hidden = true;
    }
  });

  (async () => {
    try {
      const [userList, bugList] = await Promise.all([
        api("/api/users"),
        api("/api/bugs"),
      ]);
      users = userList;
      bugs = bugList.items;
      renderUserPicker();
    } catch (err) {
      toast(`初始化失败：${err.message}`, "error");
    }
  })();
}

document.addEventListener("DOMContentLoaded", init);
