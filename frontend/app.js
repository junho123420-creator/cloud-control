"use strict";

// 외부 문자열은 모두 textContent로만 표시한다 (innerHTML 사용 금지).
// 토큰은 아래 변수(JS 메모리)에만 둔다. URL·localStorage·쿠키·console에 넣지 않는다.

const $ = (id) => document.getElementById(id);

let token = null;
// 로그아웃·재연결 후 늦게 도착한 이전 세션의 응답을 버리기 위한 번호
let session = 0;
// 확인창에 표시할 계정·리전 (서버 /api/context 결과)
let lastContext = null;
// 제어 탭에 표시할 EC2 목록
let ec2Items = [];
// 제어 요청이 진행 중이면 모든 제어 버튼을 막는다 (서버 검사는 별도로 유지).
let busy = false;
// 응답을 받지 못한 요청의 키. "인스턴스:작업" → request_id. 자동 재요청하지 않고 다음 승인 때 재사용한다.
const unconfirmed = new Map();

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

class StaleSession extends Error {}

// status 0 = 서버 응답을 받지 못함(결과 불명)
async function fetchJson(path, { method = "GET", body } = {}) {
  const mySession = session;
  if (!token) throw new ApiError(401, "APP_AUTH_REQUIRED", "관리자 토큰을 입력하세요.");
  const headers = { Accept: "application/json", Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "서버 응답을 받지 못했습니다.");
  }
  if (mySession !== session) throw new StaleSession();

  let data = null;
  try {
    data = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 처리한다.
  }
  if (mySession !== session) throw new StaleSession();

  if (!response.ok) {
    const detail = data && typeof data.detail === "object" && data.detail !== null ? data.detail : {};
    throw new ApiError(
      response.status,
      detail.code || "HTTP_ERROR",
      detail.message || `요청이 실패했습니다. (HTTP ${response.status})`,
    );
  }
  if (data === null) {
    throw new ApiError(response.status, "INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.");
  }
  return { status: response.status, data };
}

// ---- 상태 표시 ----

function setStatus(el, kind, text) {
  el.className = `status status-${kind}`;
  el.textContent = text;
  el.hidden = !text;
}

function showError(el, err) {
  setStatus(el, "error", `[${err.code}] ${err.message}`);
}

function setCount(el, kind, text) {
  el.className = `card-value card-${kind}`;
  el.textContent = text;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text ?? "-";
  if (className) td.className = className;
  return td;
}

// ---- 화면 전환 ----

const loginView = $("login-view");
const appView = $("app-view");
const appActions = $("app-actions");
const tokenInput = $("token-input");
const connectBtn = $("connect-btn");
const loginStatus = $("login-status");
const refreshBtn = $("refresh-btn");
const controlRefreshBtn = $("control-refresh-btn");
const dialog = $("confirm-dialog");

function showLogin(err) {
  loginView.hidden = false;
  appView.hidden = true;
  appActions.hidden = true;
  if (err) showError(loginStatus, err);
  else setStatus(loginStatus, "ok", "");
  tokenInput.focus();
}

function showApp() {
  loginView.hidden = true;
  appView.hidden = false;
  appActions.hidden = false;
  setStatus(loginStatus, "ok", "");
}

const tabs = document.querySelectorAll('[role="tab"]');

function selectTab(name) {
  tabs.forEach((t) => {
    const selected = t.dataset.tab === name;
    t.setAttribute("aria-selected", String(selected));
    $(`tab-${t.dataset.tab}`).hidden = !selected;
  });
}

tabs.forEach((tab) => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));

// 조회 데이터를 화면과 메모리에서 모두 지운다.
function clearData() {
  lastContext = null;
  ec2Items = [];
  unconfirmed.clear();
  busy = false;
  if (dialog.open) dialog.close("cancel");
  for (const id of ["ctx-account", "ctx-arn", "ctx-region", "last-fetched"]) $(id).textContent = "-";
  for (const id of ["count-ec2", "count-s3", "count-sg"]) setCount($(id), "idle", "-");
  for (const id of ["context-status", "ec2-status", "ec2-action-status", "s3-status", "sg-status", "control-status", "control-ec2-status", "actions-status"]) {
    setStatus($(id), "ok", "");
  }
  for (const id of ["ec2-table", "s3-table", "sg-table", "control-ec2-table", "actions-table"]) {
    const table = $(id);
    table.hidden = true;
    table.querySelector("tbody").replaceChildren();
  }
  renderUnconfirmed();
  selectTab("dashboard");
}

// ---- 리소스별 조회 ----

function renderContext(data) {
  lastContext = { account_id: data.account_id, region: data.region };
  $("ctx-account").textContent = data.account_id;
  $("ctx-arn").textContent = data.caller_arn;
  $("ctx-region").textContent = data.region;
  setStatus($("context-status"), "ok", "");
}

async function loadContext() {
  const status = $("context-status");
  setStatus(status, "loading", "계정 정보를 조회하는 중…");
  for (const id of ["ctx-account", "ctx-arn", "ctx-region"]) $(id).textContent = "…";
  try {
    renderContext((await fetchJson("/api/context")).data);
  } catch (err) {
    if (err instanceof StaleSession) return;
    lastContext = null;
    for (const id of ["ctx-account", "ctx-arn", "ctx-region"]) $(id).textContent = "조회 실패";
    handleError(status, err);
  }
}

// 공통 목록 조회: 로딩 → (오류 | 빈 목록 | 표) 중 하나로 표시한다.
async function loadList({ path, statusId, tableId, countId, emptyText, renderRows, onLoaded, onFailed }) {
  const status = $(statusId);
  const table = $(tableId);
  const count = countId ? $(countId) : null;
  table.hidden = true;
  setStatus(status, "loading", "조회하는 중…");
  if (count) setCount(count, "loading", "…");
  try {
    const { data } = await fetchJson(path);
    const tbody = table.querySelector("tbody");
    tbody.replaceChildren(...data.items.flatMap(renderRows));
    if (count) setCount(count, "ok", String(data.count));
    if (data.count === 0) {
      setStatus(status, "empty", emptyText);
    } else {
      setStatus(status, "ok", "");
      table.hidden = false;
    }
    if (onLoaded) onLoaded(data);
  } catch (err) {
    if (err instanceof StaleSession) return;
    // 실패를 0개로 표시하지 않는다.
    if (count) setCount(count, "error", "오류");
    handleError(status, err);
    if (onFailed) onFailed(err);
  }
}

// 사용 중 토큰이 거절되면(서버 토큰 변경 등) 로그아웃하고 입력 화면으로 돌아간다.
function handleError(statusEl, err) {
  if (err.status === 401) {
    logout(err);
    return;
  }
  showError(statusEl, err);
}

function renderEc2(item) {
  const tr = document.createElement("tr");
  tr.append(
    cell(item.name),
    cell(item.instance_id, "mono"),
    cell(item.state, `state state-${item.state}`),
    cell(item.instance_type),
    cell(item.private_ip, "mono"),
    cell(item.public_ip, "mono"),
    cell(item.controllable ? "허용" : "-", item.controllable ? "controllable" : "muted"),
    item.controllable ? actionButtonsCell(item) : cell("-", "muted"),
  );
  return [tr];
}

function renderS3(item) {
  const tr = document.createElement("tr");
  tr.append(cell(item.name, "mono"));
  return [tr];
}

function formatProtocol(proto) {
  if (proto === "-1") return "전체";
  return String(proto).toUpperCase();
}

function formatPorts(perm) {
  if (perm.IpProtocol === "-1" || perm.FromPort === undefined) return "전체";
  if (perm.FromPort === perm.ToPort) return String(perm.FromPort);
  return `${perm.FromPort}-${perm.ToPort}`;
}

function formatSources(perm) {
  const sources = [
    ...(perm.IpRanges || []).map((r) => r.CidrIp),
    ...(perm.Ipv6Ranges || []).map((r) => r.CidrIpv6),
    ...(perm.UserIdGroupPairs || []).map((p) => p.GroupId),
    ...(perm.PrefixListIds || []).map((p) => p.PrefixListId),
  ];
  return sources.length ? sources.join(", ") : "-";
}

function renderSg(group) {
  const perms = group.IpPermissions || [];
  if (perms.length === 0) {
    const tr = document.createElement("tr");
    const empty = cell("인바운드 규칙 없음", "muted");
    empty.colSpan = 3;
    tr.append(cell(group.GroupId, "mono"), cell(group.GroupName), empty);
    return [tr];
  }
  return perms.map((perm, i) => {
    const tr = document.createElement("tr");
    if (i === 0) {
      const id = cell(group.GroupId, "mono");
      const name = cell(group.GroupName);
      id.rowSpan = name.rowSpan = perms.length;
      tr.append(id, name);
    }
    tr.append(
      cell(formatProtocol(perm.IpProtocol)),
      cell(formatPorts(perm), "mono"),
      cell(formatSources(perm), "mono"),
    );
    return tr;
  });
}

function renderAction(log) {
  const tr = document.createElement("tr");
  tr.append(
    cell(log.created_at, "mono"),
    cell(log.instance_id, "mono"),
    cell(log.action === "start" ? "시작" : log.action === "stop" ? "중지" : log.action),
    cell(log.status, `result-${log.status}`),
    cell(log.message),
  );
  return [tr];
}

// ---- 제어 탭: 허용 대상 EC2 ----

const ACTION_LABEL = { start: "시작", stop: "중지" };

// EC2 탭과 제어 탭이 같은 버튼·확인창을 쓴다.
function actionButtonsCell(item) {
  const td = document.createElement("td");
  const wrap = document.createElement("div");
  wrap.className = "row-actions";
  for (const action of ["start", "stop"]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = ACTION_LABEL[action];
    // stopped에서만 시작, running에서만 중지. 전환 상태(pending/stopping 등)는 둘 다 비활성.
    const allowedState = action === "start" ? "stopped" : "running";
    btn.disabled = busy || item.state !== allowedState;
    btn.addEventListener("click", () => openConfirm(item, action));
    wrap.append(btn);
  }
  td.append(wrap);
  return td;
}

// 요청 진행 여부(busy)가 바뀌면 두 탭의 버튼을 다시 그린다.
function renderActionButtons() {
  $("ec2-table").querySelector("tbody").replaceChildren(...ec2Items.flatMap(renderEc2));
  renderControlEc2();
}

function renderControlEc2() {
  const status = $("control-ec2-status");
  const table = $("control-ec2-table");
  const targets = ec2Items.filter((item) => item.controllable);
  const rows = targets.map((item) => {
    const tr = document.createElement("tr");
    tr.append(cell(item.name), cell(item.instance_id, "mono"), cell(item.state, `state state-${item.state}`), actionButtonsCell(item));
    return tr;
  });
  table.querySelector("tbody").replaceChildren(...rows);
  if (rows.length === 0) {
    table.hidden = true;
    setStatus(status, "empty", "제어 허용 대상 인스턴스가 없습니다. (서버의 ALLOWED_INSTANCE_IDS)");
  } else {
    table.hidden = false;
    setStatus(status, "ok", "");
  }
}

function renderUnconfirmed() {
  const box = $("unconfirmed-box");
  if (unconfirmed.size === 0) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  const lines = [...unconfirmed.entries()].map(([key, rid]) => {
    const [instanceId, action] = key.split(":");
    return `${instanceId} ${ACTION_LABEL[action]} (요청 키 ${rid})`;
  });
  box.textContent =
    "응답을 받지 못한 요청: " + lines.join(", ") +
    ". 결과를 모르므로 자동으로 다시 보내지 않습니다. 같은 작업을 다시 승인하면 같은 요청 키로 보내 중복 실행되지 않습니다.";
  box.hidden = false;
}

// ---- 확인창과 제어 요청 ----

let pendingConfirm = null;

function openConfirm(item, action) {
  if (busy) return;
  // 결과 메시지는 버튼을 누른 탭에 표시한다.
  const statusId = $("tab-ec2").hidden ? "control-status" : "ec2-action-status";
  if (!lastContext) {
    showError($(statusId), new ApiError(0, "CONTEXT_REQUIRED", "계정 정보를 먼저 조회하세요. (새로고침)"));
    return;
  }
  pendingConfirm = { item, action, statusId };
  $("cf-account").textContent = lastContext.account_id;
  $("cf-region").textContent = lastContext.region;
  $("cf-name").textContent = item.name;
  $("cf-id").textContent = item.instance_id;
  $("cf-action").textContent = `${ACTION_LABEL[action]} (${action})`;
  $("cf-reuse").hidden = !unconfirmed.has(`${item.instance_id}:${action}`);
  dialog.returnValue = "";
  dialog.showModal();
}

dialog.addEventListener("close", () => {
  const target = pendingConfirm;
  pendingConfirm = null;
  // 취소·Esc면 아무 요청도 보내지 않는다.
  if (dialog.returnValue !== "approve" || !target) return;
  sendAction(target.item, target.action, target.statusId);
});

async function sendAction(item, action, statusId) {
  if (busy) return;
  const key = `${item.instance_id}:${action}`;
  // 승인할 때 한 번만 키를 만든다. 응답을 못 받았던 요청이 있으면 그 키를 그대로 쓴다.
  const requestId = unconfirmed.get(key) ?? crypto.randomUUID();
  const status = $(statusId);
  busy = true;
  renderActionButtons();
  setStatus(status, "loading", `${item.name} ${ACTION_LABEL[action]} 요청을 보내는 중…`);
  try {
    const { status: code, data } = await fetchJson(`/api/ec2/${encodeURIComponent(item.instance_id)}/actions`, {
      method: "POST",
      body: { action, request_id: requestId },
    });
    unconfirmed.delete(key);
    const prefix = code === 202 ? "AWS가 요청을 접수했습니다" : "이미 처리된 요청입니다";
    const text = `${prefix}: 이력 #${data.action_id}, ${data.status}${data.warning ? ` — ${data.warning}` : ""}`;
    setStatus(status, data.warning ? "warn" : "empty", text);
  } catch (err) {
    if (err instanceof StaleSession) return;
    if (err.status === 0 || err.code === "INVALID_RESPONSE") {
      // 응답 유실: 서버에서 실행됐을 수 있으므로 키를 보관하고 새 키로 자동 재요청하지 않는다.
      unconfirmed.set(key, requestId);
      setStatus(status, "warn", `[${err.code}] ${err.message} 결과를 모르므로 자동으로 다시 보내지 않습니다. ` +
        `새로고침으로 상태를 확인하고, 같은 작업을 다시 승인하면 같은 요청 키(${requestId})로 보냅니다.`);
    } else {
      // 서버가 응답했으면 결과가 이력에 반영되었으므로 키를 버린다.
      unconfirmed.delete(key);
      handleError(status, err);
    }
  } finally {
    if (token) {
      busy = false;
      renderUnconfirmed();
      renderActionButtons();
    }
  }
  if (token) await loadControl();
}

// ---- 조회 묶음 ----

function ec2ListOptions() {
  return {
    path: "/api/ec2", statusId: "ec2-status", tableId: "ec2-table", countId: "count-ec2",
    emptyText: "EC2 인스턴스가 없습니다.", renderRows: renderEc2,
    onLoaded: (data) => {
      ec2Items = data.items;
      renderControlEc2();
    },
    onFailed: (err) => {
      ec2Items = [];
      $("control-ec2-table").hidden = true;
      showError($("control-ec2-status"), err);
    },
  };
}

function loadActions() {
  return loadList({
    path: "/api/actions", statusId: "actions-status", tableId: "actions-table",
    emptyText: "제어 이력이 없습니다.", renderRows: renderAction,
  });
}

function loadLists() {
  setStatus($("control-ec2-status"), "loading", "조회하는 중…");
  return Promise.all([
    loadList(ec2ListOptions()),
    loadList({
      path: "/api/s3", statusId: "s3-status", tableId: "s3-table", countId: "count-s3",
      emptyText: "S3 버킷이 없습니다.", renderRows: renderS3,
    }),
    loadList({
      path: "/api/security-groups", statusId: "sg-status", tableId: "sg-table", countId: "count-sg",
      emptyText: "보안 그룹이 없습니다.", renderRows: renderSg,
    }),
    loadActions(),
  ]);
}

// 제어 탭의 수동 새로고침: EC2 현재 상태와 이력만 다시 읽는다.
async function loadControl() {
  const mySession = session;
  controlRefreshBtn.disabled = true;
  setStatus($("control-ec2-status"), "loading", "조회하는 중…");
  await Promise.all([loadList(ec2ListOptions()), loadActions()]);
  controlRefreshBtn.disabled = false;
  markFetched(mySession);
}

function markFetched(mySession) {
  if (mySession === session && token) $("last-fetched").textContent = new Date().toLocaleString("ko-KR");
}

// ---- 연결·조회·로그아웃 ----

async function connect(event) {
  event.preventDefault(); // 폼 제출로 토큰이 URL에 붙지 않게 한다.
  const value = tokenInput.value.trim();
  if (!value) {
    showError(loginStatus, new ApiError(0, "APP_AUTH_REQUIRED", "관리자 토큰을 입력하세요."));
    return;
  }

  session += 1;
  const mySession = session;
  token = value;
  connectBtn.disabled = true;
  setStatus(loginStatus, "loading", "토큰을 확인하는 중…");

  let context;
  try {
    context = (await fetchJson("/api/context")).data;
  } catch (err) {
    if (err instanceof StaleSession) return;
    token = null;
    connectBtn.disabled = false;
    showLogin(err);
    return;
  }

  tokenInput.value = ""; // 입력란에도 남기지 않는다.
  connectBtn.disabled = false;
  clearData();
  showApp();
  renderContext(context);
  refreshBtn.disabled = true;
  await loadLists();
  refreshBtn.disabled = false;
  markFetched(mySession);
}

async function refreshAll() {
  const mySession = session;
  refreshBtn.disabled = true;
  refreshBtn.textContent = "조회 중…";
  await Promise.all([loadContext(), loadLists()]);
  refreshBtn.disabled = false;
  refreshBtn.textContent = "조회";
  markFetched(mySession);
}

function logout(err) {
  session += 1; // 진행 중인 요청의 응답은 화면에 반영하지 않는다.
  token = null;
  tokenInput.value = "";
  connectBtn.disabled = false;
  refreshBtn.disabled = false;
  refreshBtn.textContent = "조회";
  controlRefreshBtn.disabled = false;
  clearData();
  showLogin(err);
}

$("login-form").addEventListener("submit", connect);
refreshBtn.addEventListener("click", refreshAll);
controlRefreshBtn.addEventListener("click", loadControl);
$("logout-btn").addEventListener("click", () => logout());

showLogin();
