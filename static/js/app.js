"use strict";

/* =========================================================================
 * VCF Billing Portal — 프론트엔드 (vanilla JS + Chart.js)
 * ========================================================================= */

const API_BASE = "/api";
const COLORS = { vcpu: "#2f6fed", vmem: "#12a594", vdisk: "#d98a1c" };

const state = {
  token: localStorage.getItem("vcf_billing_token") || null,
  user: null,
  period: "30d",
  monthValue: null, // period === "month" 일 때 "YYYY-MM"
  charts: {},
  overview: null, // 현재 화면(관리자 또는 사용자)에 표시 중인 AdminOverviewOut 형태 응답
  adminTenantFilter: "", // 관리자 화면의 테넌트 필터. "" = 전체(교차 확인)
  adminPage: "overview", // 관리자 메뉴 현재 탭: overview | tenants | integrations | system
  systemStatusTimer: null, // [v4.4] "시스템 상태" 탭이 열려 있는 동안만 도는 30초 자동 새로고침 타이머
  tenants: [], // 관리자: 테넌트 목록 캐시
  currentTenantDetailId: null, // 관리자: 현재 열려 있는 테넌트 상세 관리 패널의 테넌트 id
  integrationAccounts: [], // 관리자: 연동 계정 목록 캐시
  currentInventoryAccountId: null, // 관리자: 현재 열려 있는 인벤토리 패널의 연동 계정 id
  tenantModalMode: "create", // create | edit
  editingTenantId: null,
  accountModalMode: "create", // create | edit
  editingAccountId: null,
  editingProject: null, // { tenantId, projectId } - 프로젝트 생성/수정 모달에서 사용 (projectId는 생성 시 null)
  projectModalMode: "create", // create | edit - 프로젝트 생성/수정 팝업이 공유하는 모드
  // Cluster/VM Folder/VM Tag 다중 선택 상태 (id Set). 연동 계정을 전환해도 선택 내역이
  // 유지되도록 checkbox 렌더링과 분리해서 관리한다.
  projectEditPicker: { clusters: new Set(), folders: new Set(), tags: new Set() },
};

// 기간/월 선택 UI를 빠르게 연속 조작할 때, 먼저 시작된(느린) 흐름이 나중에 끝나
// 사용자의 최신 선택을 덮어쓰는 것을 막기 위한 조작 순번. period-select/month-select
// change 핸들러가 시작할 때마다 증가시키고, await 이후 계속 진행해도 되는지 확인한다.
let periodEpoch = 0;

/** 현재 state.period(+monthValue)를 API 쿼리스트링으로 변환한다. */
function periodQueryString() {
  if (state.period === "month" && state.monthValue) {
    return `period=month&month=${encodeURIComponent(state.monthValue)}`;
  }
  return `period=${state.period}`;
}

/** PDF 결산서 다운로드는 "완료된 캘린더 월" 개념이므로 특정 월 조회 중일 때만 제공한다. */
function isMonthMode() {
  return state.period === "month" && !!state.monthValue;
}

/** 인증 토큰을 포함해 PDF를 받아와 브라우저 다운로드를 트리거한다. */
async function downloadPdf(path, filenameFallback) {
  try {
    const headers = {};
    if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
    const res = await fetch(`${API_BASE}${path}`, { headers });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch (_) {
        /* no-op */
      }
      if (res.status === 401) doLogout();
      throw new Error(detail);
    }
    const blob = await res.blob();
    let filename = filenameFallback;
    const disposition = res.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    if (match) filename = match[1];

    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (err) {
    showToast(err.message || "PDF 다운로드에 실패했습니다.", "error");
  }
}

/* ---------------------------- API 헬퍼 ---------------------------- */

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {
      /* no-op */
    }
    if (res.status === 401) doLogout();
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------------------- 포맷 유틸 ---------------------------- */

function fmtMoney(value, currency) {
  const opts =
    currency === "KRW"
      ? { style: "currency", currency: "KRW", maximumFractionDigits: 0 }
      : { style: "currency", currency: currency || "USD", maximumFractionDigits: 2 };
  try {
    return new Intl.NumberFormat("ko-KR", opts).format(value || 0);
  } catch (_) {
    return `${(value || 0).toLocaleString()} ${currency || ""}`;
  }
}

function fmtNum(value, digits = 0) {
  return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: digits }).format(value || 0);
}

// 소수 시간(예: 123.4)을 "123h 24m" 형태로 표시한다. 시간만 반올림해서 보여주면
// 5분 단위로 누적되는 실제 값과 눈에 띄게 어긋나 보일 수 있어 분 단위까지 표시.
function fmtHoursMinutes(hours) {
  const totalMinutes = Math.round(Math.max(hours || 0, 0) * 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${fmtNum(h)}h ${m}m`;
}

// [v4.4] "시스템 상태" 화면용 - 바이트를 사람이 읽기 좋은 단위로.
function fmtBytes(bytes) {
  if (bytes == null) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Math.max(bytes, 0);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

// [v4.4] 초 단위 시간을 "X일 Y시간" 처럼 큰 단위 위주로 축약 표시 (가동시간/경과시간용).
function fmtDurationShort(seconds) {
  if (seconds == null) return "-";
  const s = Math.max(Math.round(seconds), 0);
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  if (days > 0) return `${days}일 ${hours}시간`;
  if (hours > 0) return `${hours}시간 ${minutes}분`;
  if (minutes > 0) return `${minutes}분`;
  return `${s}초`;
}

function fmtDateKst(isoString) {
  const d = new Date(isoString);
  return d.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" });
}

function fmtDateTimeKst(isoString) {
  if (!isoString) return "-";
  const d = new Date(isoString);
  return d.toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function syncStatusBadgeHtml(account) {
  const status = account.last_sync_status;
  // [v4.2] 마지막 동기화 시각을 배지 title(마우스 오버) 안에만 두지 않고, 항상 보이는
  // 텍스트로도 함께 표시한다 - 관리자가 굳이 마우스를 올려보지 않아도 알 수 있도록.
  const syncTimeLine = account.last_sync_at
    ? `<div class="sync-time muted small">마지막 동기화: ${fmtDateTimeKst(account.last_sync_at)}</div>`
    : "";
  if (status === "success") {
    return (
      `<span class="status-badge status-ok">✓ 연동됨</span>` +
      syncTimeLine +
      `<div class="muted small">VM ${fmtNum(account.last_sync_vm_count)}대 수집됨</div>`
    );
  }
  if (status === "error") {
    return (
      `<span class="status-badge status-error" title="${escapeHtml(account.last_sync_error || "")}">✗ 연동 실패</span>` +
      syncTimeLine
    );
  }
  return `<span class="status-badge status-pending">연동 대기</span>`;
}

function accountSaveToastMessage(verb, account) {
  // verb: "등록" | "수정"
  if (account.last_sync_status === "success") {
    return `연동 계정을 ${verb}했습니다 · 연동 확인 완료 (VM ${fmtNum(account.last_sync_vm_count)}대 수집됨)`;
  }
  if (account.last_sync_status === "error") {
    return `연동 계정을 ${verb}했지만 연동 확인에 실패했습니다: ${account.last_sync_error || ""} (정보를 확인하고 "가져오기"로 재시도하세요)`;
  }
  return `연동 계정을 ${verb}했습니다`;
}

function periodLabel(data) {
  return `${fmtDateKst(data.period_start)} ~ ${fmtDateKst(data.period_end)} (KST 기준) · 5분 단위 Power-On 시간 기준 산정`;
}

function uptimePillHtml(ratio) {
  const pct = Math.round(ratio * 100);
  const cls = pct >= 5 ? "" : "low";
  return `<span class="uptime-pill ${cls}">${pct}%</span>`;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showToast(message, kind = "") {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => (el.hidden = true), 2800);
}

/* ---------------------------- 인증 / 라우팅 ---------------------------- */

function saveSession(token, user) {
  state.token = token;
  state.user = user;
  localStorage.setItem("vcf_billing_token", token);
}

function doLogout() {
  state.token = null;
  state.user = null;
  stopSystemStatusAutoRefresh(); // [v4.4] 로그아웃 후에도 타이머가 살아남아 401 토스트를 계속 띄우는 것을 방지
  localStorage.removeItem("vcf_billing_token");
  document.getElementById("app-shell").hidden = true;
  document.getElementById("view-login").hidden = false;
}

async function boot() {
  document.getElementById("login-form").addEventListener("submit", onLoginSubmit);
  document.getElementById("logout-btn").addEventListener("click", doLogout);
  document.getElementById("refresh-btn").addEventListener("click", () => refreshCurrentView());
  document.getElementById("change-password-btn").addEventListener("click", openPasswordModal);
  document.getElementById("password-modal-close").addEventListener("click", closePasswordModal);
  document.getElementById("password-cancel-btn").addEventListener("click", closePasswordModal);
  document.getElementById("password-form").addEventListener("submit", onPasswordFormSubmit);
  document.getElementById("admin-tenant-filter").addEventListener("change", (e) => {
    state.adminTenantFilter = e.target.value;
    loadAdminView();
  });
  document.getElementById("period-select").addEventListener("change", async (e) => {
    const myEpoch = ++periodEpoch;
    state.period = e.target.value;
    const monthInput = document.getElementById("month-select");
    if (state.period === "month") {
      monthInput.hidden = false;
      if (!monthInput.value) {
        await populateAvailableMonths(monthInput);
        // populateAvailableMonths가 기다리는 동안 사용자가 이미 다른 월/기간을 선택했다면
        // (예: 기본월 채우기가 끝나기 전에 특정 월을 직접 고른 경우) 이 흐름은 더 이상
        // 최신 상태가 아니므로 여기서 state.monthValue를 덮어쓰지 않고 조용히 폐기한다.
        if (myEpoch !== periodEpoch) return;
      }
      state.monthValue = monthInput.value || null;
      if (!state.monthValue) return; // 아직 선택 전 — 값 선택 시 아래 change 리스너가 갱신
    } else {
      monthInput.hidden = true;
    }
    refreshCurrentView();
  });
  document.getElementById("month-select").addEventListener("change", (e) => {
    periodEpoch++;
    state.monthValue = e.target.value || null;
    if (state.monthValue) refreshCurrentView();
  });

  document.getElementById("drilldown-close").addEventListener("click", () => {
    document.getElementById("admin-drilldown").hidden = true;
  });
  document.getElementById("user-drilldown-close").addEventListener("click", () => {
    document.getElementById("user-drilldown").hidden = true;
  });
  document.getElementById("user-drilldown-pdf-btn").addEventListener("click", (e) => {
    const projectId = e.currentTarget.dataset.projectId;
    if (!projectId || !isMonthMode()) return;
    downloadPdf(
      `/me/projects/${projectId}/statement.pdf?month=${encodeURIComponent(state.monthValue)}`,
      `statement_${state.monthValue}.pdf`
    );
  });
  document.getElementById("drilldown-pdf-btn").addEventListener("click", (e) => {
    const projectId = e.currentTarget.dataset.projectId;
    if (!projectId || !isMonthMode()) return;
    downloadPdf(
      `/admin/projects/${projectId}/statement.pdf?month=${encodeURIComponent(state.monthValue)}`,
      `statement_${state.monthValue}.pdf`
    );
  });

  document.getElementById("rate-modal-close").addEventListener("click", closeRateModal);
  document.getElementById("rate-cancel-btn").addEventListener("click", closeRateModal);
  document.getElementById("rate-form").addEventListener("submit", onRateFormSubmit);
  document.getElementById("rate-usage-weight-enabled").addEventListener("change", updateRateUsageWeightFloorVisibility);

  // 관리자 메뉴 탭 전환
  document.querySelectorAll(".admin-nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchAdminPage(btn.dataset.adminPage));
  });

  // 테넌트 관리
  document.getElementById("tenant-create-btn").addEventListener("click", () => openTenantModal("create"));
  document.getElementById("tenant-modal-close").addEventListener("click", closeTenantModal);
  document.getElementById("tenant-cancel-btn").addEventListener("click", closeTenantModal);
  document.getElementById("tenant-form").addEventListener("submit", onTenantFormSubmit);
  document.getElementById("tenant-detail-close").addEventListener("click", () => {
    document.getElementById("tenant-detail-panel").hidden = true;
    state.currentTenantDetailId = null;
  });
  document.getElementById("tenant-detail-edit-btn").addEventListener("click", () => {
    const t = state.tenants.find((x) => x.id === state.currentTenantDetailId);
    if (t) openTenantModal("edit", t);
  });
  document.getElementById("tenant-detail-delete-btn").addEventListener("click", () => {
    if (state.currentTenantDetailId) onTenantDelete(state.currentTenantDetailId);
  });

  document.getElementById("tenant-user-form").addEventListener("submit", onTenantUserFormSubmit);

  // [v3.5] 프로젝트 추가는 더 이상 테넌트 상세 패널에 인라인 폼으로 있지 않고, "관리" 모달
  // 안의 "+ 프로젝트 추가" 버튼이 프로젝트 생성/수정 공용 팝업(project-edit-modal)을 연다.
  document.getElementById("project-create-btn").addEventListener("click", () => {
    if (state.currentTenantDetailId) openProjectModal("create", state.currentTenantDetailId);
  });
  document.getElementById("project-edit-account-select").addEventListener("change", (e) => {
    loadPickerInventory(e.target.value, "project-edit", state.projectEditPicker);
  });
  document.getElementById("project-edit-form").addEventListener("submit", onProjectEditFormSubmit);
  document.getElementById("project-edit-modal-close").addEventListener("click", closeProjectEditModal);
  document.getElementById("project-edit-cancel-btn").addEventListener("click", closeProjectEditModal);

  // 계정 연동
  document.getElementById("account-create-btn").addEventListener("click", () => openAccountModal("create"));
  document.getElementById("account-modal-close").addEventListener("click", closeAccountModal);
  document.getElementById("account-cancel-btn").addEventListener("click", closeAccountModal);
  document.getElementById("account-form").addEventListener("submit", onAccountFormSubmit);
  document.getElementById("inventory-panel-close").addEventListener("click", () => {
    document.getElementById("inventory-panel").hidden = true;
    state.currentInventoryAccountId = null;
  });

  // [v4.4] 시스템 상태
  document.getElementById("system-status-refresh-btn").addEventListener("click", loadSystemStatus);

  if (state.token) {
    try {
      state.user = await api("/auth/me");
      enterApp();
      return;
    } catch (_) {
      doLogout();
    }
  }
}

async function onLoginSubmit(e) {
  e.preventDefault();
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.hidden = true;
  try {
    const resp = await api("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    saveSession(resp.access_token, resp.user);
    enterApp();
  } catch (err) {
    errEl.textContent = err.message || "로그인에 실패했습니다.";
    errEl.hidden = false;
  }
}

function enterApp() {
  document.getElementById("view-login").hidden = true;
  document.getElementById("app-shell").hidden = false;
  document.getElementById("user-email").textContent = `${state.user.display_name} · ${state.user.email}`;

  const badge = document.getElementById("role-badge");
  const isAdmin = state.user.role === "admin";
  badge.textContent = isAdmin ? "관리자" : "일반 사용자";
  badge.classList.toggle("admin", isAdmin);

  document.getElementById("view-user").hidden = isAdmin;
  document.getElementById("view-admin").hidden = !isAdmin;
  document.getElementById("admin-tenant-filter").hidden = !isAdmin;
  document.getElementById("change-password-btn").hidden = !isAdmin;

  if (isAdmin) {
    loadTenantManagement(); // 테넌트 필터 드롭다운 채우기 + 캐시
    switchAdminPage("overview");
  } else {
    refreshCurrentView();
  }
}

function refreshCurrentView() {
  if (!state.user) return;
  if (state.user.role === "admin") {
    loadAdminView();
  } else {
    loadUserView();
  }
}

/* ---------------------------- 비밀번호 변경 (로그인 계정 본인) ---------------------------- */

function openPasswordModal() {
  document.getElementById("password-form").reset();
  document.getElementById("password-form-error").hidden = true;
  document.getElementById("password-modal").hidden = false;
}

function closePasswordModal() {
  document.getElementById("password-modal").hidden = true;
}

async function onPasswordFormSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("password-form-error");
  errEl.hidden = true;
  const currentPassword = document.getElementById("password-current").value;
  const newPassword = document.getElementById("password-new").value;
  const confirmPassword = document.getElementById("password-new-confirm").value;
  if (newPassword !== confirmPassword) {
    errEl.textContent = "새 비밀번호가 서로 일치하지 않습니다.";
    errEl.hidden = false;
    return;
  }
  try {
    await api("/auth/me/password", {
      method: "PUT",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
    closePasswordModal();
    showToast("비밀번호가 변경되었습니다.", "success");
  } catch (err) {
    errEl.textContent = err.message || "변경에 실패했습니다.";
    errEl.hidden = false;
  }
}

/** 데이터가 존재하는 캘린더 월 목록을 조회해 month input의 선택 범위/기본값을 채운다. */
async function populateAvailableMonths(monthInput) {
  const isAdmin = state.user.role === "admin";
  const path = isAdmin
    ? `/admin/months${state.adminTenantFilter ? `?tenant_id=${encodeURIComponent(state.adminTenantFilter)}` : ""}`
    : "/me/months";
  try {
    const months = await api(path); // "YYYY-MM" 최신순
    if (!months.length) return;
    monthInput.max = months[0];
    monthInput.min = months[months.length - 1];
    monthInput.value = months[0];
  } catch (err) {
    showToast(err.message, "error");
  }
}

/* ---------------------------- 일반 사용자 화면 (배정된 테넌트 범위) ---------------------------- */

// 기간/월을 빠르게 전환할 때 이전에 날아간(느린) 요청의 응답이 나중에 도착해
// 최신 화면 상태를 덮어쓰는 것을 막기 위한 요청 순번 가드.
let userViewRequestSeq = 0;

async function loadUserView() {
  const mySeq = ++userViewRequestSeq;
  let data;
  try {
    data = await api(`/me/overview?${periodQueryString()}`);
  } catch (err) {
    if (mySeq === userViewRequestSeq) showToast(err.message, "error");
    return;
  }
  if (mySeq !== userViewRequestSeq) return; // 더 최신 요청이 이미 발행됨 - 이 응답은 폐기
  state.overview = data;

  document.getElementById("user-tenant-name").textContent = `${data.tenant_name || "-"} 사용량 및 요금`;
  document.getElementById("user-period-label").textContent = periodLabel(data);
  document.getElementById("user-currency-note").textContent = data.currency_note;

  renderKpiGrid(document.getElementById("user-kpis"), [
    { label: "프로젝트 수", value: fmtNum(data.total_projects), dot: COLORS.vcpu },
    { label: "전체 VM", value: fmtNum(data.total_vms), sub: `가동 ${fmtNum(data.total_powered_on_vms)}대`, dot: COLORS.vmem },
    { label: "기간 예상 요금", value: fmtMoney(data.total_cost, data.projects[0]?.currency || "KRW"), highlight: true },
  ]);

  renderProjectComparisonChart("user-project-chart", data.projects);
  renderAggregatedDailyChart("user-daily-chart", data.projects);
  renderUserProjectTable(data.projects);

  document.getElementById("user-drilldown").hidden = true;
}

function renderUserProjectTable(projects) {
  const tbody = document.getElementById("user-project-table-body");
  if (!projects.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">등록된 프로젝트가 없습니다. 관리자에게 문의하세요.</td></tr>`;
    return;
  }
  const monthMode = isMonthMode();
  tbody.innerHTML = projects
    .map(
      (p) => `
    <tr class="clickable" data-project-id="${p.project_id}">
      <td class="vm-name">${escapeHtml(p.project_name)}</td>
      <td>${escapeHtml(p.criteria_summary)}</td>
      <td class="num">${fmtNum(p.vm_count)}</td>
      <td class="num">${fmtNum(p.powered_on_vm_count)}</td>
      <td class="num">${fmtNum(p.total_vcpu)}</td>
      <td class="num">${fmtNum(p.total_vmem_gb, 1)}</td>
      <td class="num">${fmtNum(p.total_vdisk_gb, 1)}</td>
      <td class="num strong">${fmtMoney(p.total_cost, p.currency)}</td>
      <td>${monthMode ? `<div class="row-actions"><button class="btn btn-ghost btn-sm pdf-btn" data-project-id="${p.project_id}">PDF</button></div>` : ""}</td>
    </tr>`
    )
    .join("");

  tbody.querySelectorAll("tr.clickable").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      openUserDrilldown(Number(tr.dataset.projectId));
    });
  });
  tbody.querySelectorAll(".pdf-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      downloadPdf(
        `/me/projects/${btn.dataset.projectId}/statement.pdf?month=${encodeURIComponent(state.monthValue)}`,
        `statement_${state.monthValue}.pdf`
      );
    });
  });
}

function openUserDrilldown(projectId) {
  const project = state.overview.projects.find((p) => p.project_id === projectId);
  if (!project) return;
  document.getElementById("user-drilldown").hidden = false;
  document.getElementById("user-drilldown-title").textContent = `${project.project_name} — VM별 상세`;
  renderVmTable(document.getElementById("user-drilldown-vm-table-body"), project.vms, project.currency);

  const pdfBtn = document.getElementById("user-drilldown-pdf-btn");
  pdfBtn.hidden = !isMonthMode();
  pdfBtn.dataset.projectId = projectId;
  if (isMonthMode()) pdfBtn.textContent = `PDF 결산서 다운로드 (${state.monthValue})`;
  // [v4.2] 화면 가운데 팝업(모달)으로 뜨므로 더 이상 페이지를 스크롤할 필요가 없다.
}

function renderKpiGrid(container, items) {
  container.innerHTML = items
    .map(
      (it) => `
    <div class="kpi-card ${it.highlight ? "highlight" : ""}">
      <div class="kpi-label">${it.dot ? `<span class="kpi-dot" style="background:${it.dot}"></span>` : ""}${it.label}</div>
      <div class="kpi-value">${it.value}</div>
      ${it.sub ? `<div class="kpi-sub">${it.sub}</div>` : ""}
    </div>`
    )
    .join("");
}

function renderVmTable(tbody, vms, currency) {
  if (!vms.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="muted">표시할 VM이 없습니다.</td></tr>`;
    return;
  }
  tbody.innerHTML = vms
    .map(
      (v) => `
    <tr>
      <td class="vm-name">${escapeHtml(v.vm_name)}</td>
      <td class="num">${fmtNum(v.vcpu_count)}</td>
      <td class="num">${fmtNum(v.vmem_gb, 1)}</td>
      <td class="num">${fmtNum(v.vdisk_gb, 1)}</td>
      <td class="num">${fmtHoursMinutes(v.powered_on_hours)}</td>
      <td class="num">${uptimePillHtml(v.uptime_ratio)}</td>
      <td class="num">${fmtMoney(v.vcpu_cost, currency)}</td>
      <td class="num">${fmtMoney(v.vmem_cost, currency)}</td>
      <td class="num">${fmtMoney(v.vdisk_cost, currency)}</td>
      <td class="num strong">${fmtMoney(v.total_cost, currency)}</td>
      <td class="num">${fmtUsagePct(v.avg_cpu_usage_pct)} / ${fmtUsagePct(v.avg_mem_usage_pct)}</td>
    </tr>`
    )
    .join("");
}

function fmtUsagePct(pct) {
  if (pct === null || pct === undefined) return "-";
  return `${fmtNum(pct, 0)}%`;
}

/* ---------------------------- 차트 (관리자/사용자 공용) ---------------------------- */

function renderProjectComparisonChart(canvasId, projects) {
  const currency = projects[0]?.currency || "KRW";
  const data = {
    labels: projects.map((p) => p.project_name),
    datasets: [
      { label: "vCPU", data: projects.map((p) => p.total_vcpu_cost), backgroundColor: COLORS.vcpu, stack: "s" },
      { label: "vMEM", data: projects.map((p) => p.total_vmem_cost), backgroundColor: COLORS.vmem, stack: "s" },
      { label: "vDisk", data: projects.map((p) => p.total_vdisk_cost), backgroundColor: COLORS.vdisk, stack: "s" },
    ],
  };
  buildOrUpdateChart(canvasId, "bar", data, currency, true);
}

function renderAggregatedDailyChart(canvasId, projects) {
  const currency = projects[0]?.currency || "KRW";
  const byDate = new Map();
  for (const p of projects) {
    for (const d of p.daily) {
      const row = byDate.get(d.date) || { vcpu: 0, vmem: 0, vdisk: 0 };
      row.vcpu += d.vcpu_cost;
      row.vmem += d.vmem_cost;
      row.vdisk += d.vdisk_cost;
      byDate.set(d.date, row);
    }
  }
  const dates = [...byDate.keys()].sort();
  const data = {
    labels: dates.map((d) => d.slice(5)),
    datasets: [
      { label: "vCPU", data: dates.map((d) => byDate.get(d).vcpu), backgroundColor: COLORS.vcpu, stack: "s" },
      { label: "vMEM", data: dates.map((d) => byDate.get(d).vmem), backgroundColor: COLORS.vmem, stack: "s" },
      { label: "vDisk", data: dates.map((d) => byDate.get(d).vdisk), backgroundColor: COLORS.vdisk, stack: "s" },
    ],
  };
  buildOrUpdateChart(canvasId, "bar", data, currency, true);
}

function buildOrUpdateChart(canvasId, type, data, currency, stacked) {
  if (state.charts[canvasId]) {
    state.charts[canvasId].data = data;
    state.charts[canvasId].update();
    return;
  }
  const ctx = document.getElementById(canvasId).getContext("2d");
  state.charts[canvasId] = new Chart(ctx, {
    type,
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true, padding: 16, font: { size: 11 } } },
        tooltip: {
          backgroundColor: "#1a2233",
          padding: 10,
          callbacks: {
            label: (item) => `${item.dataset.label}: ${fmtMoney(item.raw, currency)}`,
          },
        },
      },
      scales: {
        x: { stacked: !!stacked, grid: { display: false }, ticks: { font: { size: 11 } } },
        y: {
          stacked: !!stacked,
          grid: { color: "#eef1f6" },
          ticks: { font: { size: 11 }, callback: (v) => fmtMoney(v, currency) },
        },
      },
    },
  });
}

/* ---------------------------- 관리자 화면 - 메뉴 전환 ---------------------------- */

function switchAdminPage(page) {
  state.adminPage = page;
  document.querySelectorAll(".admin-nav-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.adminPage === page));
  document.querySelectorAll(".admin-page").forEach((sec) => {
    sec.hidden = sec.id !== `admin-page-${page}`;
  });
  // [v4.4] "시스템 상태" 탭에서 다른 탭으로 이동하면 자동 새로고침 타이머부터 정리한다 -
  // 안 그러면 화면에 보이지도 않는 탭을 위해 30초마다 불필요한 API 호출이 계속 나간다.
  if (page !== "system") stopSystemStatusAutoRefresh();

  if (page === "overview") {
    loadAdminView();
  } else if (page === "tenants") {
    loadTenantManagement();
    if (!state.integrationAccounts.length) loadIntegrationAccounts();
  } else if (page === "integrations") {
    loadIntegrationAccounts();
  } else if (page === "system") {
    loadSystemStatus();
    startSystemStatusAutoRefresh();
  }
}

/* ---------------------------- 관리자 화면 - 시스템 상태 (v4.4) ---------------------------- */

function startSystemStatusAutoRefresh() {
  stopSystemStatusAutoRefresh();
  state.systemStatusTimer = setInterval(() => {
    // 그 사이 다른 탭으로 넘어갔다면(방어적 이중 체크) 조용히 멈춘다.
    if (state.adminPage !== "system") {
      stopSystemStatusAutoRefresh();
      return;
    }
    loadSystemStatus();
  }, 30000);
}

function stopSystemStatusAutoRefresh() {
  if (state.systemStatusTimer) {
    clearInterval(state.systemStatusTimer);
    state.systemStatusTimer = null;
  }
}

async function loadSystemStatus() {
  let data;
  try {
    data = await api("/admin/system-status");
  } catch (err) {
    showToast(err.message, "error");
    return;
  }

  const dbEngineLabel = data.db.engine === "postgresql" ? "PostgreSQL" : "SQLite";
  const connLabel = data.db.active_connections == null ? "" : ` · 활성 커넥션 ${fmtNum(data.db.active_connections)}개`;

  renderKpiGrid(document.getElementById("system-status-kpis"), [
    { label: "DB 전체 크기", value: fmtBytes(data.db.size_bytes), sub: `${dbEngineLabel}${connLabel}`, highlight: true },
    { label: "API 프로세스 메모리", value: `${fmtNum(Math.round(data.api_process.memory_rss_mb))} MB`, sub: "api 컨테이너 자기 자신" },
    { label: "API 프로세스 CPU", value: `${data.api_process.cpu_percent.toFixed(1)}%`, sub: "직전 측정 구간 평균" },
    { label: "API 프로세스 가동시간", value: fmtDurationShort(data.api_process.uptime_seconds), sub: "마지막 재시작 이후" },
  ]);

  const tbody = document.getElementById("system-status-table-body");
  tbody.innerHTML = data.db.tables
    .map(
      (t) => `
    <tr>
      <td><code>${escapeHtml(t.name)}</code></td>
      <td>${fmtNum(t.row_count)}</td>
      <td>${fmtBytes(t.size_bytes)}</td>
    </tr>`
    )
    .join("");

  const dbNote = document.getElementById("system-status-db-note");
  if (data.db.newest_usage_sample_at) {
    dbNote.textContent =
      `사용량 데이터(power_samples) 보관 기간: ${fmtDateTimeKst(data.db.oldest_usage_sample_at)} ~ ${fmtDateTimeKst(data.db.newest_usage_sample_at)}` +
      (data.db.engine === "sqlite" ? " · SQLite 폴백 사용 중 - 테이블별 크기는 지원되지 않아 행 수만 표시합니다." : "");
  } else {
    dbNote.textContent = "아직 적재된 사용량 데이터(power_samples)가 없습니다.";
  }

  const c = data.collector;
  const summary = document.getElementById("system-status-collector-summary");
  if (c.total_accounts === 0) {
    summary.innerHTML = `<span class="status-badge status-pending">등록된 연동 계정 없음</span>`;
  } else {
    const badges = [];
    if (c.accounts_with_error > 0) {
      badges.push(`<span class="status-badge status-error">✗ 연동 실패 ${fmtNum(c.accounts_with_error)}건</span>`);
    }
    if (c.accounts_never_synced > 0) {
      badges.push(`<span class="status-badge status-pending">동기화 대기 ${fmtNum(c.accounts_never_synced)}건</span>`);
    }
    if (c.accounts_with_error === 0 && c.accounts_never_synced === 0) {
      badges.push(`<span class="status-badge status-ok">✓ 전체 정상</span>`);
    }
    const lastSyncLine = c.last_sync_at
      ? `마지막 동기화: ${fmtDateTimeKst(c.last_sync_at)} (${fmtDurationShort(c.seconds_since_last_sync)} 전) · 수집 주기 ${fmtNum(c.interval_minutes)}분`
      : `아직 한 번도 동기화되지 않았습니다 · 수집 주기 ${fmtNum(c.interval_minutes)}분`;
    summary.innerHTML = `${badges.join(" ")}<div class="muted small" style="margin-top:6px;">등록된 연동 계정 ${fmtNum(c.total_accounts)}개 · ${lastSyncLine}</div>`;
  }
}

/* ---------------------------- 관리자 화면 - 개요 ---------------------------- */

// loadUserView와 동일한 이유의 요청 순번 가드 (예: 초기 30d 응답이 늦게 도착해
// 그 사이 사용자가 연 드릴다운/월 조회 화면을 덮어쓰는 것을 방지).
let adminViewRequestSeq = 0;

async function loadAdminView() {
  const mySeq = ++adminViewRequestSeq;
  const tenantQuery = state.adminTenantFilter ? `&tenant_id=${encodeURIComponent(state.adminTenantFilter)}` : "";
  let data;
  try {
    data = await api(`/admin/overview?${periodQueryString()}${tenantQuery}`);
  } catch (err) {
    if (mySeq === adminViewRequestSeq) showToast(err.message, "error");
    return;
  }
  if (mySeq !== adminViewRequestSeq) return; // 더 최신 요청이 이미 발행됨 - 이 응답은 폐기
  state.overview = data;

  document.getElementById("admin-overview-title").textContent = data.tenant_name
    ? `${data.tenant_name} — 프로젝트 현황`
    : "전체 프로젝트 현황 (전체 테넌트 교차 확인)";
  document.getElementById("admin-period-label").textContent = periodLabel(data);
  document.getElementById("admin-currency-note").textContent = data.currency_note;

  renderKpiGrid(document.getElementById("admin-kpis"), [
    { label: "프로젝트 수", value: fmtNum(data.total_projects), dot: COLORS.vcpu },
    { label: "전체 VM", value: fmtNum(data.total_vms), sub: `가동 ${fmtNum(data.total_powered_on_vms)}대`, dot: COLORS.vmem },
    { label: "전체 기간 요금", value: fmtMoney(data.total_cost, data.projects[0]?.currency || "KRW"), highlight: true },
  ]);

  renderProjectComparisonChart("admin-project-chart", data.projects);
  renderAggregatedDailyChart("admin-daily-chart", data.projects);
  renderAdminProjectTable(data.projects);
  renderTenantSummary(data.tenant_id, data.tenant_summaries || []);

  document.getElementById("admin-drilldown").hidden = true;
}

/** [v4.2] "전체 테넌트 (교차 확인)" 조회일 때만 프로젝트 목록 위에 테넌트별 집계를 보여준다.
 * 특정 테넌트로 필터링한 조회(tenant_id != null)에서는 어차피 테넌트가 하나뿐이라 숨긴다. */
function renderTenantSummary(tenantId, summaries) {
  const card = document.getElementById("admin-tenant-summary-card");
  if (tenantId != null || !summaries.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const tbody = document.getElementById("admin-tenant-summary-body");
  tbody.innerHTML = summaries
    .map(
      (t) => `
    <tr>
      <td class="vm-name">${escapeHtml(t.tenant_name)} <span class="muted">(${escapeHtml(t.tenant_key)})</span></td>
      <td class="num">${fmtNum(t.project_count)}</td>
      <td class="num">${fmtNum(t.vm_count)}</td>
      <td class="num">${fmtNum(t.powered_on_vm_count)}</td>
      <td class="num strong">${fmtMoney(t.total_cost, t.currency_note)}</td>
    </tr>`
    )
    .join("");
}

function renderAdminProjectTable(projects) {
  const tbody = document.getElementById("admin-project-table-body");
  if (!projects.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="muted">등록된 프로젝트가 없습니다.</td></tr>`;
    return;
  }
  const monthMode = isMonthMode();
  tbody.innerHTML = projects
    .map(
      (p) => `
    <tr class="clickable" data-project-id="${p.project_id}">
      <td class="vm-name">${escapeHtml(p.project_name)} <span class="muted">(${escapeHtml(p.project_key)})</span></td>
      <td>${escapeHtml(p.tenant_name)}</td>
      <td>${escapeHtml(p.criteria_summary)}</td>
      <td class="num">${fmtNum(p.vm_count)}</td>
      <td class="num">${fmtNum(p.powered_on_vm_count)}</td>
      <td class="num">${fmtNum(p.total_vcpu)}</td>
      <td class="num">${fmtNum(p.total_vmem_gb, 1)}</td>
      <td class="num">${fmtNum(p.total_vdisk_gb, 1)}</td>
      <td class="num">${fmtMoney(p.rate.vcpu_rate_per_hour, p.currency)} / ${fmtMoney(p.rate.vmem_rate_per_hour_gb, p.currency)} / ${fmtMoney(p.rate.vdisk_rate_per_hour_gb, p.currency)}${p.rate.usage_weight_enabled ? ' <span class="muted" title="사용량 가중치 반영 중">(가중치)</span>' : ""}</td>
      <td class="num strong">${fmtMoney(p.total_cost, p.currency)}</td>
      <td>
        <div class="row-actions">
          <button class="btn btn-ghost btn-sm rate-edit-btn" data-project-id="${p.project_id}">요금 설정</button>
          ${monthMode ? `<button class="btn btn-ghost btn-sm pdf-btn" data-project-id="${p.project_id}">PDF</button>` : ""}
        </div>
      </td>
    </tr>`
    )
    .join("");

  tbody.querySelectorAll("tr.clickable").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      openDrilldown(Number(tr.dataset.projectId));
    });
  });
  tbody.querySelectorAll(".rate-edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => openRateModal(Number(btn.dataset.projectId)));
  });
  tbody.querySelectorAll(".pdf-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      downloadPdf(
        `/admin/projects/${btn.dataset.projectId}/statement.pdf?month=${encodeURIComponent(state.monthValue)}`,
        `statement_${state.monthValue}.pdf`
      );
    });
  });
}

function openDrilldown(projectId) {
  const project = state.overview.projects.find((p) => p.project_id === projectId);
  if (!project) return;
  document.getElementById("admin-drilldown").hidden = false;
  document.getElementById("drilldown-title").textContent = `${project.project_name} — VM별 상세`;
  renderVmTable(document.getElementById("drilldown-vm-table-body"), project.vms, project.currency);

  const pdfBtn = document.getElementById("drilldown-pdf-btn");
  pdfBtn.hidden = !isMonthMode();
  pdfBtn.dataset.projectId = projectId;
  if (isMonthMode()) pdfBtn.textContent = `PDF 결산서 다운로드 (${state.monthValue})`;
  // [v4.2] 화면 가운데 팝업(모달)으로 뜨므로 더 이상 페이지를 스크롤할 필요가 없다.
}

/* ---------------------------- 요금 설정 모달 ---------------------------- */

function openRateModal(projectId) {
  const project = state.overview.projects.find((p) => p.project_id === projectId);
  if (!project) return;
  document.getElementById("rate-modal").dataset.projectId = projectId;
  document.getElementById("rate-modal-project").textContent = project.project_name;
  document.getElementById("rate-vcpu").value = project.rate.vcpu_rate_per_hour;
  document.getElementById("rate-vmem").value = project.rate.vmem_rate_per_hour_gb;
  document.getElementById("rate-vdisk").value = project.rate.vdisk_rate_per_hour_gb;
  document.getElementById("rate-currency").value = project.rate.currency;
  document.getElementById("rate-usage-weight-enabled").checked = Boolean(project.rate.usage_weight_enabled);
  document.getElementById("rate-usage-weight-floor").value = project.rate.usage_weight_floor_pct ?? 30;
  updateRateUsageWeightFloorVisibility();
  document.getElementById("rate-form-error").hidden = true;
  document.getElementById("rate-modal").hidden = false;
}

function updateRateUsageWeightFloorVisibility() {
  const enabled = document.getElementById("rate-usage-weight-enabled").checked;
  document.getElementById("rate-usage-weight-floor-row").hidden = !enabled;
}

function closeRateModal() {
  document.getElementById("rate-modal").hidden = true;
}

async function onRateFormSubmit(e) {
  e.preventDefault();
  const projectId = document.getElementById("rate-modal").dataset.projectId;
  const payload = {
    vcpu_rate_per_hour: Number(document.getElementById("rate-vcpu").value),
    vmem_rate_per_hour_gb: Number(document.getElementById("rate-vmem").value),
    vdisk_rate_per_hour_gb: Number(document.getElementById("rate-vdisk").value),
    currency: document.getElementById("rate-currency").value,
    usage_weight_enabled: document.getElementById("rate-usage-weight-enabled").checked,
    usage_weight_floor_pct: Number(document.getElementById("rate-usage-weight-floor").value),
  };
  const errEl = document.getElementById("rate-form-error");
  try {
    await api(`/admin/projects/${projectId}/rates`, { method: "PUT", body: JSON.stringify(payload) });
    closeRateModal();
    showToast("요금 단위가 저장되었습니다.", "success");
    await loadAdminView();
  } catch (err) {
    errEl.textContent = err.message || "저장에 실패했습니다.";
    errEl.hidden = false;
  }
}

/* ---------------------------- 테넌트 관리 (관리자) ---------------------------- */

async function loadTenantManagement() {
  try {
    const tenants = await api("/admin/tenants");
    state.tenants = tenants;
    renderTenantTable(tenants);
    populateAdminTenantFilter(tenants);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function renderTenantTable(tenants) {
  const tbody = document.getElementById("tenant-table-body");
  if (!tenants.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">등록된 테넌트가 없습니다.</td></tr>`;
    return;
  }
  tbody.innerHTML = tenants
    .map(
      (t) => `
    <tr>
      <td class="vm-name">${escapeHtml(t.name)}</td>
      <td>${escapeHtml(t.key)}</td>
      <td class="num">${fmtNum(t.project_count)}</td>
      <td class="num">${fmtNum(t.user_count)}</td>
      <td>
        <div class="row-actions">
          <button class="btn btn-ghost btn-sm tenant-manage-btn" data-tenant-id="${t.id}">관리</button>
          <button class="btn btn-ghost btn-sm tenant-edit-btn" data-tenant-id="${t.id}">수정</button>
          <button class="btn btn-ghost btn-sm danger tenant-delete-btn" data-tenant-id="${t.id}">삭제</button>
        </div>
      </td>
    </tr>`
    )
    .join("");

  tbody.querySelectorAll(".tenant-manage-btn").forEach((btn) => {
    btn.addEventListener("click", () => openTenantDetail(Number(btn.dataset.tenantId)));
  });
  tbody.querySelectorAll(".tenant-edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const t = state.tenants.find((x) => x.id === Number(btn.dataset.tenantId));
      if (t) openTenantModal("edit", t);
    });
  });
  tbody.querySelectorAll(".tenant-delete-btn").forEach((btn) => {
    btn.addEventListener("click", () => onTenantDelete(Number(btn.dataset.tenantId)));
  });
}

function populateAdminTenantFilter(tenants) {
  const sel = document.getElementById("admin-tenant-filter");
  const current = sel.value;
  sel.innerHTML =
    `<option value="">전체 테넌트 (교차 확인)</option>` +
    tenants.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
  sel.value = tenants.some((t) => String(t.id) === current) ? current : "";
  state.adminTenantFilter = sel.value;
}

function openTenantModal(mode, tenant) {
  state.tenantModalMode = mode;
  state.editingTenantId = tenant ? tenant.id : null;
  document.getElementById("tenant-form").reset();
  document.getElementById("tenant-form-error").hidden = true;
  const keyInput = document.getElementById("tenant-key");
  const title = document.getElementById("tenant-modal-title");
  const submitBtn = document.getElementById("tenant-form-submit-btn");
  if (mode === "edit" && tenant) {
    title.textContent = "테넌트 수정";
    submitBtn.textContent = "저장";
    keyInput.value = tenant.key;
    keyInput.disabled = true;
    document.getElementById("tenant-name").value = tenant.name;
    document.getElementById("tenant-description").value = tenant.description || "";
  } else {
    title.textContent = "새 테넌트 생성";
    submitBtn.textContent = "생성";
    keyInput.disabled = false;
  }
  document.getElementById("tenant-modal").hidden = false;
}

function closeTenantModal() {
  document.getElementById("tenant-modal").hidden = true;
}

async function onTenantFormSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("tenant-form-error");
  try {
    if (state.tenantModalMode === "edit") {
      const payload = {
        name: document.getElementById("tenant-name").value.trim(),
        description: document.getElementById("tenant-description").value.trim(),
      };
      await api(`/admin/tenants/${state.editingTenantId}`, { method: "PUT", body: JSON.stringify(payload) });
      showToast("테넌트가 수정되었습니다.", "success");
    } else {
      const payload = {
        key: document.getElementById("tenant-key").value.trim(),
        name: document.getElementById("tenant-name").value.trim(),
        description: document.getElementById("tenant-description").value.trim(),
      };
      await api("/admin/tenants", { method: "POST", body: JSON.stringify(payload) });
      showToast("테넌트가 생성되었습니다.", "success");
    }
    closeTenantModal();
    await loadTenantManagement();
    if (state.currentTenantDetailId) await openTenantDetail(state.currentTenantDetailId);
  } catch (err) {
    errEl.textContent = err.message || "저장에 실패했습니다.";
    errEl.hidden = false;
  }
}

async function onTenantDelete(tenantId) {
  const t = state.tenants.find((x) => x.id === tenantId);
  const name = t ? t.name : "";
  if (!confirm(`"${name}" 테넌트와 하위 프로젝트/사용자 계정이 모두 삭제됩니다. 계속할까요?`)) return;
  try {
    await api(`/admin/tenants/${tenantId}`, { method: "DELETE" });
    showToast("테넌트가 삭제되었습니다.", "success");
    if (state.currentTenantDetailId === tenantId) {
      document.getElementById("tenant-detail-panel").hidden = true;
      state.currentTenantDetailId = null;
    }
    await loadTenantManagement();
    loadAdminView();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function openTenantDetail(tenantId) {
  try {
    if (!state.integrationAccounts.length) await loadIntegrationAccounts();
    const detail = await api(`/admin/tenants/${tenantId}`);
    state.currentTenantDetailId = tenantId;
    renderTenantDetail(detail);
    // [v3.5] "관리" 버튼을 누르면 이제 페이지 하단에 패널을 펼치는 대신, 화면 중앙에
    // 팝업(모달)으로 뜬다 - tenant-detail-panel은 CSS상 modal-backdrop이 되었다.
    document.getElementById("tenant-detail-panel").hidden = false;
  } catch (err) {
    showToast(err.message, "error");
  }
}

function renderTenantDetail(detail) {
  document.getElementById("tenant-detail-name").textContent = `${detail.name} (${detail.key})`;

  const projBody = document.getElementById("tenant-project-table-body");
  projBody.innerHTML = detail.projects.length
    ? detail.projects
        .map(
          (p) => `
      <tr>
        <td>${escapeHtml(p.name)} <span class="muted">(${escapeHtml(p.key)})</span></td>
        <td>${escapeHtml(p.criteria_summary)}</td>
        <td class="num">${fmtNum(p.vm_count)}</td>
        <td>${escapeHtml(p.owner_email) || "-"}</td>
        <td>
          <div class="row-actions">
            <button class="btn btn-ghost btn-sm project-edit-btn" data-project-id="${p.id}">수정</button>
            <button class="btn btn-ghost btn-sm danger project-delete-btn" data-project-id="${p.id}">삭제</button>
          </div>
        </td>
      </tr>`
        )
        .join("")
    : `<tr><td colspan="5" class="muted">등록된 프로젝트가 없습니다.</td></tr>`;

  projBody.querySelectorAll(".project-edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const p = detail.projects.find((x) => x.id === Number(btn.dataset.projectId));
      if (p) openProjectModal("edit", detail.id, p);
    });
  });
  projBody.querySelectorAll(".project-delete-btn").forEach((btn) => {
    btn.addEventListener("click", () => onProjectDelete(detail.id, Number(btn.dataset.projectId)));
  });

  const userBody = document.getElementById("tenant-user-table-body");
  userBody.innerHTML = detail.users.length
    ? detail.users
        .map(
          (u) => `<tr><td>${escapeHtml(u.email)}</td><td>${escapeHtml(u.display_name) || "-"}</td><td>${u.role === "admin" ? "관리자" : "일반"}</td></tr>`
        )
        .join("")
    : `<tr><td colspan="3" class="muted">등록된 사용자가 없습니다.</td></tr>`;

  document.getElementById("tenant-user-form").reset();
  document.getElementById("tenant-user-form-error").hidden = true;
}

async function refreshTenantDetailAndList() {
  const id = state.currentTenantDetailId;
  await loadTenantManagement();
  if (id) await openTenantDetail(id);
}

async function onProjectDelete(tenantId, projectId) {
  if (!confirm("이 프로젝트를 삭제하시겠습니까? 매핑되어 있던 VM은 매칭이 해제됩니다.")) return;
  try {
    await api(`/admin/tenants/${tenantId}/projects/${projectId}`, { method: "DELETE" });
    showToast("프로젝트가 삭제되었습니다.", "success");
    await refreshTenantDetailAndList();
    loadAdminView();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function onTenantUserFormSubmit(e) {
  e.preventDefault();
  const tenantId = state.currentTenantDetailId;
  if (!tenantId) return;
  const payload = {
    email: document.getElementById("tenant-user-email").value.trim(),
    password: document.getElementById("tenant-user-password").value,
    display_name: document.getElementById("tenant-user-name").value.trim(),
    role: "user",
  };
  const errEl = document.getElementById("tenant-user-form-error");
  try {
    await api(`/admin/tenants/${tenantId}/users`, { method: "POST", body: JSON.stringify(payload) });
    showToast("사용자 계정이 생성되었습니다.", "success");
    await refreshTenantDetailAndList();
  } catch (err) {
    errEl.textContent = err.message || "생성에 실패했습니다.";
    errEl.hidden = false;
  }
}

/* ---------------------------- 프로젝트 생성/수정 모달 ---------------------------- */

// [v3.5] "프로젝트 추가"(테넌트 상세 관리 모달 안의 "+ 프로젝트 추가" 버튼)와 "프로젝트
// 수정"이 같은 팝업(project-edit-modal)을 mode만 다르게 공유한다 - 테넌트 생성/수정이
// tenant-modal 하나를 공유하는 것과 동일한 패턴. project가 없으면(mode="create") 빈
// 값으로, 있으면(mode="edit") 기존 값으로 채운다.
function openProjectModal(mode, tenantId, project) {
  state.projectModalMode = mode;
  state.editingProject = { tenantId, projectId: project ? project.id : null };
  state.projectEditPicker = {
    clusters: new Set(project ? project.criteria.clusters.map((c) => c.id) : []),
    folders: new Set(project ? project.criteria.folders.map((f) => f.id) : []),
    tags: new Set(project ? project.criteria.tags.map((t) => t.id) : []),
  };

  const titleEl = document.getElementById("project-edit-modal-title");
  const keyRow = document.getElementById("project-edit-key-row");
  const keyInput = document.getElementById("project-edit-key-input");
  const submitBtn = document.getElementById("project-edit-submit-btn");

  if (mode === "edit" && project) {
    titleEl.textContent = `프로젝트 수정 — ${project.name}`;
    keyRow.hidden = true;
    keyInput.required = false;
    submitBtn.textContent = "저장";
    document.getElementById("project-edit-name-input").value = project.name;
    document.getElementById("project-edit-description").value = project.description || "";
    document.getElementById("project-edit-owner-email").value = project.owner_email || "";
  } else {
    titleEl.textContent = "새 프로젝트 생성";
    keyRow.hidden = false;
    keyInput.required = true;
    keyInput.value = "";
    submitBtn.textContent = "추가";
    document.getElementById("project-edit-name-input").value = "";
    document.getElementById("project-edit-description").value = "";
    document.getElementById("project-edit-owner-email").value = "";
  }
  document.getElementById("project-edit-form-error").hidden = true;

  const accSelect = document.getElementById("project-edit-account-select");
  populateAccountSelect(accSelect);
  accSelect.value = "";
  loadPickerInventory("", "project-edit", state.projectEditPicker);

  document.getElementById("project-edit-modal").hidden = false;
}

function closeProjectEditModal() {
  document.getElementById("project-edit-modal").hidden = true;
  state.editingProject = null;
  state.projectModalMode = null;
}

async function onProjectEditFormSubmit(e) {
  e.preventDefault();
  const editing = state.editingProject;
  if (!editing) return;
  const errEl = document.getElementById("project-edit-form-error");
  const isCreate = state.projectModalMode === "create";
  const payload = {
    name: document.getElementById("project-edit-name-input").value.trim(),
    description: document.getElementById("project-edit-description").value.trim(),
    owner_email: document.getElementById("project-edit-owner-email").value.trim(),
    cluster_ids: Array.from(state.projectEditPicker.clusters),
    folder_ids: Array.from(state.projectEditPicker.folders),
    tag_ids: Array.from(state.projectEditPicker.tags),
  };
  if (isCreate) {
    payload.key = document.getElementById("project-edit-key-input").value.trim();
  }
  try {
    if (isCreate) {
      await api(`/admin/tenants/${editing.tenantId}/projects`, { method: "POST", body: JSON.stringify(payload) });
      showToast("프로젝트가 추가되었습니다.", "success");
    } else {
      await api(`/admin/tenants/${editing.tenantId}/projects/${editing.projectId}`, { method: "PUT", body: JSON.stringify(payload) });
      showToast("프로젝트가 수정되었습니다.", "success");
    }
    closeProjectEditModal();
    await refreshTenantDetailAndList();
    loadAdminView();
  } catch (err) {
    errEl.textContent = err.message || "저장에 실패했습니다.";
    errEl.hidden = false;
  }
}

/* ---------------------------- Cluster/VM Folder/VM Tag 다중 선택 위젯 ---------------------------- */

function populateAccountSelect(selectEl) {
  selectEl.innerHTML =
    `<option value="">연동 계정 선택...</option>` +
    state.integrationAccounts.map((a) => `<option value="${a.id}">${escapeHtml(a.name)}${a.is_mock ? " (데모)" : ""}</option>`).join("");
}

/** 인벤토리 트리를 Cluster/Folder 평면 목록(Datacenter 이름 포함)+Tag 목록으로 변환한다. */
function flattenInventory(inventory) {
  const clusters = [];
  const folders = [];
  for (const vc of inventory.vcenters) {
    for (const dc of vc.datacenters) {
      for (const c of dc.clusters) clusters.push({ id: c.id, label: `${dc.name} / ${c.name}`, vm_count: c.vm_count });
      for (const f of dc.folders) folders.push({ id: f.id, label: f.path, vm_count: f.vm_count });
    }
  }
  const tags = inventory.tags.map((t) => ({ id: t.id, label: t.label }));
  return { clusters, folders, tags };
}

function renderChecklist(container, items, selectedSet, labelFn) {
  if (!items.length) {
    container.innerHTML = `<p class="muted small">등록된 항목이 없습니다.</p>`;
    return;
  }
  container.innerHTML = items
    .map(
      (item) => `
    <label class="check-item">
      <input type="checkbox" value="${item.id}" ${selectedSet.has(item.id) ? "checked" : ""} />
      ${escapeHtml(labelFn(item))}
    </label>`
    )
    .join("");
  container.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = Number(cb.value);
      if (cb.checked) selectedSet.add(id);
      else selectedSet.delete(id);
    });
  });
}

/** 지정한 연동 계정의 인벤토리를 조회해 (containerPrefix)-cluster-checks 등 3개 체크리스트를 채운다.
 * accountId가 비어 있으면 안내 문구만 표시한다. selected는 계정을 전환해도 유지되는 선택 상태다. */
async function loadPickerInventory(accountId, containerPrefix, selected) {
  const clusterEl = document.getElementById(`${containerPrefix}-cluster-checks`);
  const folderEl = document.getElementById(`${containerPrefix}-folder-checks`);
  const tagEl = document.getElementById(`${containerPrefix}-tag-checks`);
  if (!accountId) {
    const msg = `<p class="muted small">연동 계정을 선택하세요.</p>`;
    clusterEl.innerHTML = msg;
    folderEl.innerHTML = msg;
    tagEl.innerHTML = msg;
    return;
  }
  try {
    const inv = await api(`/admin/integration-accounts/${accountId}/inventory`);
    const flat = flattenInventory(inv);
    renderChecklist(clusterEl, flat.clusters, selected.clusters, (c) => `${c.label} (VM ${c.vm_count}대)`);
    renderChecklist(folderEl, flat.folders, selected.folders, (f) => `${f.label} (VM ${f.vm_count}대)`);
    renderChecklist(tagEl, flat.tags, selected.tags, (t) => t.label);
  } catch (err) {
    showToast(err.message, "error");
  }
}

/* ---------------------------- 계정 연동 (관리자) ---------------------------- */

async function loadIntegrationAccounts() {
  try {
    const accounts = await api("/admin/integration-accounts");
    state.integrationAccounts = accounts;
    renderAccountTable(accounts);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function renderAccountTable(accounts) {
  const tbody = document.getElementById("account-table-body");
  if (!accounts.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">등록된 연동 계정이 없습니다.</td></tr>`;
    return;
  }
  tbody.innerHTML = accounts
    .map(
      (a) => `
    <tr>
      <td class="vm-name">${escapeHtml(a.name)} ${a.is_mock ? '<span class="uptime-pill">데모</span>' : ""}</td>
      <td>VCF Operations</td>
      <td>${escapeHtml(a.base_url)}</td>
      <td>${escapeHtml(a.username)}</td>
      <td>${syncStatusBadgeHtml(a)}</td>
      <td class="num">${fmtNum(a.vcenter_count)}</td>
      <td class="num">${fmtNum(a.vm_count)}</td>
      <td>
        <div class="row-actions">
          <button class="btn btn-ghost btn-sm account-sync-btn" data-account-id="${a.id}">가져오기</button>
          <button class="btn btn-ghost btn-sm account-inventory-btn" data-account-id="${a.id}">인벤토리</button>
          <button class="btn btn-ghost btn-sm account-edit-btn" data-account-id="${a.id}">수정</button>
          <button class="btn btn-ghost btn-sm danger account-delete-btn" data-account-id="${a.id}">삭제</button>
        </div>
      </td>
    </tr>`
    )
    .join("");

  tbody.querySelectorAll(".account-sync-btn").forEach((btn) => {
    btn.addEventListener("click", () => onAccountSync(Number(btn.dataset.accountId), btn));
  });
  tbody.querySelectorAll(".account-inventory-btn").forEach((btn) => {
    btn.addEventListener("click", () => openInventoryPanel(Number(btn.dataset.accountId)));
  });
  tbody.querySelectorAll(".account-edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const a = state.integrationAccounts.find((x) => x.id === Number(btn.dataset.accountId));
      if (a) openAccountModal("edit", a);
    });
  });
  tbody.querySelectorAll(".account-delete-btn").forEach((btn) => {
    btn.addEventListener("click", () => onAccountDelete(Number(btn.dataset.accountId)));
  });
}

async function onAccountSync(accountId, btnEl) {
  const originalLabel = btnEl ? btnEl.textContent : "";
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "가져오는 중...";
  }
  try {
    const result = await api(`/admin/integration-accounts/${accountId}/sync`, { method: "POST" });
    showToast(result.message, result.status === "success" ? "success" : "error");
    await loadIntegrationAccounts();
    // 인벤토리 패널을 이미 이 계정으로 열어둔 상태라면 방금 가져온 내용으로 갱신한다.
    if (state.currentInventoryAccountId === accountId) {
      openInventoryPanel(accountId);
    }
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = originalLabel;
    }
  }
}

function openAccountModal(mode, account) {
  state.accountModalMode = mode;
  state.editingAccountId = account ? account.id : null;
  document.getElementById("account-form").reset();
  document.getElementById("account-form-error").hidden = true;
  const title = document.getElementById("account-modal-title");
  const pwInput = document.getElementById("account-password");
  const pwHint = document.getElementById("account-password-hint");
  if (mode === "edit" && account) {
    title.textContent = "연동 계정 수정";
    document.getElementById("account-kind").value = account.kind;
    document.getElementById("account-name").value = account.name;
    document.getElementById("account-base-url").value = account.base_url;
    document.getElementById("account-username").value = account.username;
    document.getElementById("account-auth-source").value = account.auth_source;
    document.getElementById("account-verify-ssl").checked = account.verify_ssl;
    pwInput.required = false;
    pwHint.hidden = false;
  } else {
    title.textContent = "새 연동 계정";
    document.getElementById("account-auth-source").value = "local";
    document.getElementById("account-verify-ssl").checked = true;
    pwInput.required = true;
    pwHint.hidden = true;
  }
  document.getElementById("account-modal").hidden = false;
}

function closeAccountModal() {
  document.getElementById("account-modal").hidden = true;
}

async function onAccountFormSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("account-form-error");
  const basePayload = {
    kind: document.getElementById("account-kind").value,
    name: document.getElementById("account-name").value.trim(),
    base_url: document.getElementById("account-base-url").value.trim(),
    username: document.getElementById("account-username").value.trim(),
    auth_source: document.getElementById("account-auth-source").value.trim() || "local",
    verify_ssl: document.getElementById("account-verify-ssl").checked,
  };
  const password = document.getElementById("account-password").value;
  try {
    let saved;
    if (state.accountModalMode === "edit") {
      const payload = { ...basePayload };
      if (password) payload.password = password;
      saved = await api(`/admin/integration-accounts/${state.editingAccountId}`, { method: "PUT", body: JSON.stringify(payload) });
      showToast(accountSaveToastMessage("수정", saved), saved.last_sync_status === "error" ? "error" : "success");
    } else {
      saved = await api("/admin/integration-accounts", { method: "POST", body: JSON.stringify({ ...basePayload, password }) });
      showToast(accountSaveToastMessage("등록", saved), saved.last_sync_status === "error" ? "error" : "success");
    }
    closeAccountModal();
    await loadIntegrationAccounts();
  } catch (err) {
    errEl.textContent = err.message || "저장에 실패했습니다.";
    errEl.hidden = false;
  }
}

async function onAccountDelete(accountId) {
  const a = state.integrationAccounts.find((x) => x.id === accountId);
  if (!confirm(`"${a ? a.name : ""}" 연동 계정과 수집된 인벤토리(vCenter~VM)가 모두 삭제됩니다. 계속할까요?`)) return;
  try {
    await api(`/admin/integration-accounts/${accountId}`, { method: "DELETE" });
    showToast("연동 계정이 삭제되었습니다.", "success");
    if (state.currentInventoryAccountId === accountId) {
      document.getElementById("inventory-panel").hidden = true;
      state.currentInventoryAccountId = null;
    }
    await loadIntegrationAccounts();
    loadAdminView();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function openInventoryPanel(accountId) {
  try {
    const inv = await api(`/admin/integration-accounts/${accountId}/inventory`);
    state.currentInventoryAccountId = accountId;
    document.getElementById("inventory-panel-title").textContent = `${inv.integration_account_name} — 인벤토리 (VM ${inv.vm_count}대)`;
    renderInventoryTree(inv);
    document.getElementById("inventory-panel").hidden = false;
    // [v4.2] 화면 가운데 팝업(모달)으로 뜨므로 더 이상 페이지를 스크롤할 필요가 없다.
  } catch (err) {
    showToast(err.message, "error");
  }
}

function renderInventoryTree(inv) {
  const treeEl = document.getElementById("inventory-tree");
  if (!inv.vcenters.length) {
    treeEl.innerHTML = `<p class="muted">아직 수집된 인벤토리가 없습니다. 최대 5분 후 자동으로 수집됩니다.</p>`;
  } else {
    treeEl.innerHTML = inv.vcenters
      .map(
        (vc) => `
      <div class="inv-vcenter">
        <div class="inv-vcenter-name">🖥 ${escapeHtml(vc.name)}</div>
        ${vc.datacenters
          .map(
            (dc) => `
          <div class="inv-datacenter">
            <div class="inv-datacenter-name">${escapeHtml(dc.name)}</div>
            <div class="inv-dc-cols">
              <div class="inv-col">
                <div class="inv-col-title">Cluster</div>
                ${dc.clusters.length ? dc.clusters.map((c) => `<div class="inv-item">${escapeHtml(c.name)} <span class="muted">(VM ${c.vm_count}대)</span></div>`).join("") : '<div class="muted small">-</div>'}
              </div>
              <div class="inv-col">
                <div class="inv-col-title">VM Folder</div>
                ${dc.folders.length ? dc.folders.map((f) => `<div class="inv-item">${escapeHtml(f.path)} <span class="muted">(VM ${f.vm_count}대)</span></div>`).join("") : '<div class="muted small">-</div>'}
              </div>
            </div>
          </div>`
          )
          .join("")}
      </div>`
      )
      .join("");
  }
  const tagEl = document.getElementById("inventory-tag-list");
  tagEl.innerHTML = inv.tags.length
    ? inv.tags.map((t) => `<span class="tag-chip">${escapeHtml(t.label)}</span>`).join("")
    : `<p class="muted small">등록된 태그가 없습니다.</p>`;
}

/* ---------------------------- 시작 ---------------------------- */

document.addEventListener("DOMContentLoaded", boot);
