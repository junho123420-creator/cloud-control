"use strict";

// 외부 문자열은 모두 textContent로만 표시한다 (innerHTML 사용 금지).

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function fetchJson(path) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "서버에 연결하지 못했습니다.");
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    // JSON이 아닌 응답은 아래에서 처리한다.
  }
  if (!response.ok) {
    const detail = body && typeof body.detail === "object" ? body.detail : {};
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

// ---- 탭 ----

const tabs = document.querySelectorAll('[role="tab"]');
tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    tabs.forEach((t) => {
      const selected = t === tab;
      t.setAttribute("aria-selected", String(selected));
      $(`tab-${t.dataset.tab}`).hidden = !selected;
    });
  });
});

// ---- 리소스별 조회 ----

async function loadContext() {
  const status = $("context-status");
  setStatus(status, "loading", "계정 정보를 조회하는 중…");
  for (const id of ["ctx-account", "ctx-arn", "ctx-region"]) $(id).textContent = "…";
  try {
    const data = await fetchJson("/api/context");
    $("ctx-account").textContent = data.account_id;
    $("ctx-arn").textContent = data.caller_arn;
    $("ctx-region").textContent = data.region;
    setStatus(status, "ok", "");
  } catch (err) {
    for (const id of ["ctx-account", "ctx-arn", "ctx-region"]) $(id).textContent = "조회 실패";
    showError(status, err);
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
    // 실패를 0개로 표시하지 않는다.
    setCount(count, "error", "오류");
    showError(status, err);
  }
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

// ---- 새로고침 ----

const refreshBtn = $("refresh-btn");

async function refreshAll() {
  refreshBtn.disabled = true;
  refreshBtn.textContent = "조회 중…";
  await Promise.all([
    loadContext(),
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
  $("last-fetched").textContent = new Date().toLocaleString("ko-KR");
  refreshBtn.disabled = false;
  refreshBtn.textContent = "조회";
}

refreshBtn.addEventListener("click", refreshAll);
refreshAll();
