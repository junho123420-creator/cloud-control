"use strict";

// 외부 문자열은 모두 textContent로만 표시한다 (innerHTML 사용 금지).
// 토큰은 아래 변수(JS 메모리)에만 둔다. URL·localStorage·쿠키·console에 넣지 않는다.

const $ = (id) => document.getElementById(id);

let token = null;
// 로그아웃·재연결 후 늦게 도착한 이전 세션의 응답을 버리기 위한 번호
let session = 0;

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

class StaleSession extends Error {}

async function fetchJson(path) {
  const mySession = session;
  if (!token) throw new ApiError(401, "APP_AUTH_REQUIRED", "관리자 토큰을 입력하세요.");
  let response;
  try {
    response = await fetch(path, {
      headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "서버에 연결하지 못했습니다.");
  }
  if (mySession !== session) throw new StaleSession();

  let body = null;
  try {
    body = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 처리한다.
  }
  if (mySession !== session) throw new StaleSession();

  if (!response.ok) {
    const detail = body && typeof body.detail === "object" && body.detail !== null ? body.detail : {};
    throw new ApiError(
      response.status,
      detail.code || "HTTP_ERROR",
      detail.message || `요청이 실패했습니다. (HTTP ${response.status})`,
    );
  }
  if (body === null) {
    throw new ApiError(response.status, "INVALID_RESPONSE", "서버 응답을 해석하지 못했습니다.");
  }
  return body;
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

// 조회 데이터를 화면에서 모두 지운다.
function clearData() {
  for (const id of ["ctx-account", "ctx-arn", "ctx-region", "last-fetched"]) $(id).textContent = "-";
  for (const id of ["count-ec2", "count-s3", "count-sg"]) setCount($(id), "idle", "-");
  for (const id of ["context-status", "ec2-status", "s3-status", "sg-status"]) setStatus($(id), "ok", "");
  for (const id of ["ec2-table", "s3-table", "sg-table"]) {
    const table = $(id);
    table.hidden = true;
    table.querySelector("tbody").replaceChildren();
  }
  selectTab("dashboard");
}

// ---- 리소스별 조회 ----

function renderContext(data) {
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
    renderContext(await fetchJson("/api/context"));
  } catch (err) {
    if (err instanceof StaleSession) return;
    for (const id of ["ctx-account", "ctx-arn", "ctx-region"]) $(id).textContent = "조회 실패";
    handleError(status, err);
  }
}

// 공통 목록 조회: 로딩 → (오류 | 빈 목록 | 표) 중 하나로 표시한다.
async function loadList({ path, statusId, tableId, countId, emptyText, renderRows }) {
  const status = $(statusId);
  const table = $(tableId);
  const count = $(countId);
  table.hidden = true;
  setStatus(status, "loading", "조회하는 중…");
  setCount(count, "loading", "…");
  try {
    const data = await fetchJson(path);
    const tbody = table.querySelector("tbody");
    tbody.replaceChildren(...data.items.flatMap(renderRows));
    setCount(count, "ok", String(data.count));
    if (data.count === 0) {
      setStatus(status, "empty", emptyText);
    } else {
      setStatus(status, "ok", "");
      table.hidden = false;
    }
  } catch (err) {
    if (err instanceof StaleSession) return;
    // 실패를 0개로 표시하지 않는다.
    setCount(count, "error", "오류");
    handleError(status, err);
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

function loadLists() {
  return Promise.all([
    loadList({
      path: "/api/ec2", statusId: "ec2-status", tableId: "ec2-table", countId: "count-ec2",
      emptyText: "EC2 인스턴스가 없습니다.", renderRows: renderEc2,
    }),
    loadList({
      path: "/api/s3", statusId: "s3-status", tableId: "s3-table", countId: "count-s3",
      emptyText: "S3 버킷이 없습니다.", renderRows: renderS3,
    }),
    loadList({
      path: "/api/security-groups", statusId: "sg-status", tableId: "sg-table", countId: "count-sg",
      emptyText: "보안 그룹이 없습니다.", renderRows: renderSg,
    }),
  ]);
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
    context = await fetchJson("/api/context");
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
  clearData();
  showLogin(err);
}

$("login-form").addEventListener("submit", connect);
refreshBtn.addEventListener("click", refreshAll);
$("logout-btn").addEventListener("click", () => logout());

showLogin();
