"use strict";

/* =========================================================================
 * VCF Billing Portal — 프론트엔드 (vanilla JS + Chart.js)
 * ========================================================================= */

const API_BASE = "/api";
const COLORS = { vcpu: "#2f6fed", vmem: "#12a594", vdisk: "#d98a1c" };

// [v4.7] 저사용 VM 다운사이징 권고 임계값 - CPU/MEM 평균 사용률이 둘 다 아래이고,
// 기간의 절반 이상 Power-On 상태였던 VM만 후보로 본다(잠깐 켰다 끈 VM을 낮은
// 사용률로 오인해 권고하는 것을 방지). 관리자별 조정 UI는 아직 없음 - 필요해지면
// 프로젝트별 RateCard처럼 설정 가능하게 확장할 수 있다.
const DOWNSIZING_CPU_THRESHOLD_PCT = 20;
const DOWNSIZING_MEM_THRESHOLD_PCT = 30;
const DOWNSIZING_MIN_UPTIME_RATIO = 0.5;

const state = {
  token: localStorage.getItem("vcf_billing_token") || null,
  user: null,
  period: "30d",
  monthValue: null, // period === "month" 일 때 "YYYY-MM"
  charts: {},
  overview: null, // 현재 화면(관리자 또는 사용자)에 표시 중인 AdminOverviewOut 형태 응답
  adminTenantFilter: "", // 관리자 화면의 테넌트 필터. "" = 전체(교차 확인)
  adminPage: "overview", // 관리자 메뉴 현재 탭: overview | projects | downsizing | tenants | users | integrations | system | database
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
  // [v4.5] 사용자 관리
  users: [], // 관리자: 전체 사용자 목록 캐시
  userModalMode: "create", // create | edit
  editingUserId: null,
  resettingUserId: null, // 현재 열려 있는 "비밀번호 초기화" 모달의 대상 사용자 id
  // [v4.8] 로컬 DB 설정 팝업이 마지막으로 확인한 엔진 종류 (sqlite면 비밀번호 변경 UI를 숨김)
  localDbSetupIsSqlite: false,
  // [v4.9] 초기 설정 마법사 진행 상태 - wizardActive는 지금 마법사 흐름 중인지(하위 팝업이
  // 앞을 가리고 있어도 true 유지), wizardPendingReturn은 하위 팝업(로컬/외부 DB 설정)을
  // 닫았을 때 마법사의 어디로 돌아갈지("finish" 또는 다음 단계 번호)를 기억한다.
  wizardActive: false,
  wizardPendingReturn: null,
  // [v4.9] 외부 PostgreSQL 연결 팝업(#external-db-modal)은 Billing DB/Operations DB
  // 양쪽에서 공용으로 열리므로, 지금 어느 쪽을 대상으로 하는지 기억해둔다.
  externalDbTarget: "billing",
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

  // [v4.7] VM 드릴다운 팝업 검색/페이지네이션 - 관리자/사용자 공통 로직(kind로만 구분).
  document.getElementById("drilldown-search").addEventListener("input", (e) => {
    drilldownPaging.admin.query = e.target.value;
    drilldownPaging.admin.page = 1;
    renderVmTablePaged("admin");
  });
  document.getElementById("drilldown-pager").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-page]");
    if (!btn || btn.disabled) return;
    drilldownPaging.admin.page += btn.dataset.page === "prev" ? -1 : 1;
    renderVmTablePaged("admin");
  });
  document.getElementById("user-drilldown-search").addEventListener("input", (e) => {
    drilldownPaging.user.query = e.target.value;
    drilldownPaging.user.page = 1;
    renderVmTablePaged("user");
  });
  document.getElementById("user-drilldown-pager").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-page]");
    if (!btn || btn.disabled) return;
    drilldownPaging.user.page += btn.dataset.page === "prev" ? -1 : 1;
    renderVmTablePaged("user");
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

  // [v4.5] 테넌트 상세 패널의 사용자 생성 인라인 폼은 "사용자 관리" 메뉴로 분리되었다 -
  // 관련 바인딩은 아래 "사용자 관리" 섹션 참고.

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

  // [v4.5] 사용자 관리
  document.getElementById("user-create-btn").addEventListener("click", () => openUserModal("create"));
  document.getElementById("user-modal-close").addEventListener("click", closeUserModal);
  document.getElementById("user-cancel-btn").addEventListener("click", closeUserModal);
  document.getElementById("user-form").addEventListener("submit", onUserFormSubmit);
  document.getElementById("user-role-select").addEventListener("change", updateUserTenantFieldVisibility);
  document.getElementById("user-reset-password-modal-close").addEventListener("click", closeResetPasswordModal);
  document.getElementById("user-reset-password-cancel-btn").addEventListener("click", closeResetPasswordModal);
  document.getElementById("user-reset-password-form").addEventListener("submit", onResetPasswordFormSubmit);

  // [v4.6→v4.7] 데이터베이스 - "현재 상태"/"외부 DB 연결"은 각각 버튼으로 여는 별도 팝업
  document.getElementById("database-status-open-btn").addEventListener("click", openDatabaseStatusModal);
  document.getElementById("database-status-refresh-btn").addEventListener("click", loadDatabaseOverview);
  document.getElementById("database-status-modal-close").addEventListener("click", closeDatabaseStatusModal);
  document.getElementById("database-export-btn").addEventListener("click", onDatabaseExport);
  document.getElementById("external-db-open-btn").addEventListener("click", () => openExternalDbModal("billing"));
  document.getElementById("external-db-modal-close").addEventListener("click", closeExternalDbModal);
  document.getElementById("external-db-test-btn").addEventListener("click", onExternalDbTest);
  document.getElementById("external-db-form").addEventListener("submit", onExternalDbMigrateSubmit);
  document.getElementById("external-db-migrate-copy-btn").addEventListener("click", onCopyExternalDbUrl);

  // [v4.8] 로컬 DB 설정(비밀번호 변경/전체 초기화) 팝업 - "데이터베이스" 탭의 "현재 DB 상태"
  // 팝업과 초기 설정 마법사 2단계에서 공용으로 연다.
  document.getElementById("database-status-local-setup-btn").addEventListener("click", () => {
    closeDatabaseStatusModal();
    openLocalDbSetupModal();
  });
  document.getElementById("local-db-setup-modal-close").addEventListener("click", closeLocalDbSetupModal);
  document.getElementById("local-db-setup-form").addEventListener("submit", onLocalDbSetupFormSubmit);

  // [v4.9] Operations DB - "데이터베이스" 탭 전용 진입점 (마법사 3단계와 별개로 언제든 재설정 가능)
  document.getElementById("operations-database-status-open-btn").addEventListener("click", openOperationsDatabaseStatusModal);
  document.getElementById("operations-database-status-refresh-btn").addEventListener("click", loadOperationsDatabaseOverview);
  document.getElementById("operations-database-status-modal-close").addEventListener("click", closeOperationsDatabaseStatusModal);
  document.getElementById("operations-database-local-setup-open-btn").addEventListener("click", () => openOperationsLocalDbModal());
  document.getElementById("operations-local-db-modal-close").addEventListener("click", closeOperationsLocalDbModal);
  document.getElementById("operations-local-db-form").addEventListener("submit", onOperationsLocalDbFormSubmit);
  document.getElementById("operations-external-db-open-btn").addEventListener("click", () => openExternalDbModal("operations"));

  // [v4.8→v4.9] 최초 로그인 설정 마법사 (1단계: 관리자 비밀번호 변경 / 2단계: Billing DB /
  // 3단계: Operations DB) - 하위 팝업(로컬·외부 DB 설정)을 열 때는 마법사 팝업 자체를
  // 잠시 숨기고, 그 팝업이 닫히면 wizardReturnFromSubModal()이 다음 단계로 이어간다.
  document.getElementById("wizard-password-form").addEventListener("submit", onWizardPasswordSubmit);
  document.getElementById("wizard-password-skip-btn").addEventListener("click", () => showWizardStep(2));

  document.getElementById("wizard-billing-local-btn").addEventListener("click", () => {
    document.getElementById("initial-db-setup-modal").hidden = true;
    state.wizardPendingReturn = 3;
    openLocalDbSetupModal();
  });
  document.getElementById("wizard-billing-external-btn").addEventListener("click", () => {
    document.getElementById("initial-db-setup-modal").hidden = true;
    state.wizardPendingReturn = 3;
    openExternalDbModal("billing");
  });
  document.getElementById("wizard-step2-back-btn").addEventListener("click", () => showWizardStep(1));
  document.getElementById("wizard-step2-skip-btn").addEventListener("click", () => showWizardStep(3));

  document.getElementById("wizard-ops-local-btn").addEventListener("click", () => {
    document.getElementById("initial-db-setup-modal").hidden = true;
    state.wizardPendingReturn = "finish";
    openOperationsLocalDbModal();
  });
  document.getElementById("wizard-ops-external-btn").addEventListener("click", () => {
    document.getElementById("initial-db-setup-modal").hidden = true;
    state.wizardPendingReturn = "finish";
    openExternalDbModal("operations");
  });
  document.getElementById("wizard-step3-back-btn").addEventListener("click", () => showWizardStep(2));
  document.getElementById("wizard-step3-skip-btn").addEventListener("click", finishWizard);

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
  // [v4.5] 본인 비밀번호 변경은 이제 관리자뿐 아니라 로그인한 모든 사용자에게 노출한다
  // (백엔드 PUT /api/auth/me/password는 애초부터 역할과 무관하게 본인만 바꿀 수 있었음).
  document.getElementById("change-password-btn").hidden = false;

  if (isAdmin) {
    loadTenantManagement(); // 테넌트 필터 드롭다운 채우기 + 캐시
    switchAdminPage("overview");
    checkInitialDbSetupGate(); // [v4.8] 최초 로그인 시 1회만 로컬/외부 DB 선택 게이트 표시
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
  let forecast = null;
  try {
    // [v4.7] "이번 달 예상 청구액"은 선택된 조회 기간과 무관하게 항상 필요하므로 함께 가져온다.
    // forecast 쪽은 실패해도(예: 일시적 오류) 개요 화면 전체가 죽지 않도록 별도로 처리한다.
    const [overviewData, forecastData] = await Promise.all([
      api(`/me/overview?${periodQueryString()}`),
      api("/me/forecast").catch(() => null),
    ]);
    data = overviewData;
    forecast = forecastData;
  } catch (err) {
    if (mySeq === userViewRequestSeq) showToast(err.message, "error");
    return;
  }
  if (mySeq !== userViewRequestSeq) return; // 더 최신 요청이 이미 발행됨 - 이 응답은 폐기
  state.overview = data;

  document.getElementById("user-tenant-name").textContent = `${data.tenant_name || "-"} 사용량 및 요금`;
  document.getElementById("user-period-label").textContent = periodLabel(data);
  document.getElementById("user-currency-note").textContent = data.currency_note;

  const currency = data.projects[0]?.currency || "KRW";
  renderKpiGrid(document.getElementById("user-kpis"), [
    { label: "프로젝트 수", value: fmtNum(data.total_projects), dot: COLORS.vcpu },
    { label: "전체 VM", value: fmtNum(data.total_vms), sub: `가동 ${fmtNum(data.total_powered_on_vms)}대`, dot: COLORS.vmem },
    { label: "기간 예상 요금", value: fmtMoney(data.total_cost, currency), highlight: true },
    periodChangeKpi(data, currency),
    forecastKpi(forecast),
  ]);

  renderProjectComparisonChart("user-project-chart", data.projects);
  renderAggregatedDailyChart("user-daily-chart", data.projects);
  renderUserProjectTable(data.projects);
  renderDownsizingTable(
    document.getElementById("user-downsizing-table-body"),
    document.getElementById("user-downsizing-note"),
    computeDownsizingCandidates(data.projects),
    false
  );

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

  drilldownPaging.user = { vms: project.vms, currency: project.currency, query: "", page: 1 };
  document.getElementById("user-drilldown-search").value = "";
  renderVmTablePaged("user");

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

function renderVmTable(tbody, vms, currency, emptyMessage = "표시할 VM이 없습니다.") {
  if (!vms.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="muted">${escapeHtml(emptyMessage)}</td></tr>`;
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

/* ---------------------------- [v4.7] VM 드릴다운 팝업 - 페이지네이션 + 이름 검색 ---------------------------- */

const VM_DRILLDOWN_PAGE_SIZE = 20;
// 관리자/사용자 드릴다운 팝업은 서로 다른 DOM(prefix)을 쓰지만 로직은 완전히 동일하므로
// kind("admin" | "user")별로 현재 프로젝트의 VM 전체 목록 + 검색어 + 현재 페이지만 들고 있는다.
const drilldownPaging = {
  admin: { vms: [], currency: "KRW", query: "", page: 1 },
  user: { vms: [], currency: "KRW", query: "", page: 1 },
};

function _drilldownPrefix(kind) {
  return kind === "admin" ? "drilldown" : "user-drilldown";
}

function renderVmTablePaged(kind) {
  const d = drilldownPaging[kind];
  const prefix = _drilldownPrefix(kind);
  const q = d.query.trim().toLowerCase();
  const filtered = q ? d.vms.filter((v) => v.vm_name.toLowerCase().includes(q)) : d.vms;

  const totalPages = Math.max(1, Math.ceil(filtered.length / VM_DRILLDOWN_PAGE_SIZE));
  d.page = Math.min(Math.max(d.page, 1), totalPages);
  const startIdx = (d.page - 1) * VM_DRILLDOWN_PAGE_SIZE;
  const pageItems = filtered.slice(startIdx, startIdx + VM_DRILLDOWN_PAGE_SIZE);

  const emptyMessage = q ? `"${d.query}"와(과) 일치하는 VM이 없습니다.` : "표시할 VM이 없습니다.";
  renderVmTable(document.getElementById(`${prefix}-vm-table-body`), pageItems, d.currency, emptyMessage);
  renderVmPager(prefix, d.page, totalPages, filtered.length);
}

function renderVmPager(prefix, page, totalPages, totalCount) {
  const el = document.getElementById(`${prefix}-pager`);
  if (!totalCount) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `
    <button type="button" class="btn btn-ghost btn-sm" data-page="prev" ${page <= 1 ? "disabled" : ""}>‹ 이전</button>
    <span class="pager-info">${page} / ${totalPages}페이지 · 총 ${fmtNum(totalCount)}대</span>
    <button type="button" class="btn btn-ghost btn-sm" data-page="next" ${page >= totalPages ? "disabled" : ""}>다음 ›</button>
  `;
}

function fmtUsagePct(pct) {
  if (pct === null || pct === undefined) return "-";
  return `${fmtNum(pct, 0)}%`;
}

/* ---------------------------- [v4.7] 직전 기간 대비 증감 / 예상 청구액 / 다운사이징 권고 ---------------------------- */

function fmtChangePct(pct) {
  if (pct === null || pct === undefined) return "—";
  const arrow = pct > 0 ? "▲" : pct < 0 ? "▼" : "‒";
  return `${arrow} ${Math.abs(pct).toFixed(1)}%`;
}

/** AdminOverviewOut(admin/me 공용)에서 "직전 기간 대비" KPI 카드 하나를 만든다. */
function periodChangeKpi(data, currency) {
  const sub =
    data.previous_period_total_cost > 0
      ? `직전 기간 합계: ${fmtMoney(data.previous_period_total_cost, currency)}`
      : "직전 기간에 사용량 없음";
  return { label: "직전 기간 대비", value: fmtChangePct(data.period_over_period_change_pct), sub };
}

/** MonthForecastOut에서 "이번 달 예상 청구액" KPI 카드 하나를 만든다. forecast가 아직 로딩
 * 전이거나 실패했으면 null을 받아 안내만 표시한다. */
function forecastKpi(forecast) {
  if (!forecast) {
    return { label: "이번 달 예상 청구액", value: "-", sub: "불러오지 못했습니다" };
  }
  return {
    label: "이번 달 예상 청구액",
    value: fmtMoney(forecast.forecast_total_cost, forecast.currency_note === "혼합" ? "KRW" : forecast.currency_note),
    sub: `현재까지 ${fmtMoney(forecast.mtd_total_cost, forecast.currency_note === "혼합" ? "KRW" : forecast.currency_note)} · ${forecast.days_elapsed.toFixed(1)}/${forecast.days_in_month}일 경과`,
  };
}

/** projects(ProjectUsageOut[])에서 다운사이징 후보 VM을 뽑아 사용률 낮은 순으로 정렬한다. */
function computeDownsizingCandidates(projects) {
  const candidates = [];
  for (const p of projects) {
    for (const v of p.vms) {
      if (v.uptime_ratio < DOWNSIZING_MIN_UPTIME_RATIO) continue;
      if (v.avg_cpu_usage_pct == null || v.avg_mem_usage_pct == null) continue;
      if (v.avg_cpu_usage_pct < DOWNSIZING_CPU_THRESHOLD_PCT && v.avg_mem_usage_pct < DOWNSIZING_MEM_THRESHOLD_PCT) {
        candidates.push({
          ...v,
          project_name: p.project_name,
          tenant_name: p.tenant_name,
          currency: p.currency,
        });
      }
    }
  }
  candidates.sort((a, b) => a.avg_cpu_usage_pct + a.avg_mem_usage_pct - (b.avg_cpu_usage_pct + b.avg_mem_usage_pct));
  return candidates;
}

function renderDownsizingTable(tbody, noteEl, candidates, includeTenant) {
  const thresholdNote = `CPU 평균 사용률 ${DOWNSIZING_CPU_THRESHOLD_PCT}% 미만 및 MEM 평균 사용률 ${DOWNSIZING_MEM_THRESHOLD_PCT}% 미만이면서, 기간의 절반 이상 Power-On 상태였던 VM이 대상입니다 (사용률 데이터가 없는 VM은 제외).`;
  if (!candidates.length) {
    tbody.innerHTML = `<tr><td colspan="${includeTenant ? 7 : 6}" class="muted">현재 조회 기간 기준 다운사이징 권고 대상이 없습니다.</td></tr>`;
    noteEl.textContent = thresholdNote;
    return;
  }
  noteEl.textContent = `${candidates.length}대 권고 · ${thresholdNote}`;
  tbody.innerHTML = candidates
    .map(
      (v) => `
    <tr>
      <td class="vm-name">${escapeHtml(v.vm_name)}</td>
      <td>${escapeHtml(v.project_name)}</td>
      ${includeTenant ? `<td>${escapeHtml(v.tenant_name)}</td>` : ""}
      <td class="num">${fmtUsagePct(v.avg_cpu_usage_pct)}</td>
      <td class="num">${fmtUsagePct(v.avg_mem_usage_pct)}</td>
      <td class="num">${uptimePillHtml(v.uptime_ratio)}</td>
      <td class="num strong">${fmtMoney(v.total_cost, v.currency)}</td>
    </tr>`
    )
    .join("");
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

  // [v4.9] "프로젝트"/"저성능 VM" 탭은 기존 개요 화면에 있던 카드를 그대로 옮긴 것뿐이라,
  // 데이터 소스가 동일한 GET /admin/overview(+forecast) 응답이다 - loadAdminView()가
  // admin-project-table-body/admin-downsizing-table-body를 채우는데, 이 두 tbody가
  // 이제 어느 탭의 section 안에 있든(개요/프로젝트/저성능 VM) 그대로 동작한다.
  if (page === "overview" || page === "projects" || page === "downsizing") {
    loadAdminView();
  } else if (page === "tenants") {
    loadTenantManagement();
    if (!state.integrationAccounts.length) loadIntegrationAccounts();
  } else if (page === "users") {
    loadUserManagement();
    if (!state.tenants.length) loadTenantManagement(); // 사용자 생성/수정 모달의 테넌트 선택지용
  } else if (page === "integrations") {
    loadIntegrationAccounts();
  } else if (page === "system") {
    loadSystemStatus();
    startSystemStatusAutoRefresh();
  }
  // [v4.7] "데이터베이스" 탭 자체는 메뉴(버튼) 3개만 보여주는 정적 화면이라 탭 진입 시
  // 별도로 불러올 데이터가 없다 - "현재 DB 상태" 팝업을 열 때만 openDatabaseStatusModal()이
  // loadDatabaseOverview()를 호출한다.
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

/* ---------------------------- 관리자 화면 - 데이터베이스 (v4.6, v4.7에서 팝업 구조로 재구성) ---------------------------- */

function openDatabaseStatusModal() {
  document.getElementById("database-status-modal").hidden = false;
  loadDatabaseOverview();
}

function closeDatabaseStatusModal() {
  document.getElementById("database-status-modal").hidden = true;
}

/** [v4.9] Billing DB(target="billing", 기본값)와 Operations DB(target="operations")
 * 양쪽에서 공용으로 여는 팝업 - 제목/설명만 바꿔 끼우고, 실제 제출 대상 엔드포인트는
 * onExternalDbMigrateSubmit()이 state.externalDbTarget을 보고 고른다. */
function openExternalDbModal(target = "billing") {
  state.externalDbTarget = target;
  const isOps = target === "operations";
  document.getElementById("external-db-modal-title").textContent = isOps
    ? "Operations DB - 외부 PostgreSQL 연결"
    : "Billing DB - 외부 PostgreSQL 연결";
  document.getElementById("external-db-modal-desc").textContent = isOps
    ? "VM 인벤토리/전원상태 데이터 접속 테스트 및 마이그레이션"
    : "테넌트/프로젝트/요금 데이터 접속 테스트 및 마이그레이션";
  document.getElementById("external-db-form").reset();
  document.getElementById("external-db-form").hidden = false;
  document.getElementById("external-db-test-result").hidden = true;
  document.getElementById("external-db-form-error").hidden = true;
  document.getElementById("external-db-migrate-result").hidden = true;
  document.getElementById("external-db-modal").hidden = false;
}

function closeExternalDbModal() {
  document.getElementById("external-db-modal").hidden = true;
  wizardReturnFromSubModal();
  state.externalDbTarget = "billing";
}

/* ---------------------------- [v4.8→v4.9] 최초 로그인 설정 마법사 ---------------------------- */

/** admin으로 로그인할 때마다 호출 - 최초 1회(아직 안 봤을 때)만 마법사를 띄운다. */
async function checkInitialDbSetupGate() {
  try {
    const status = await api("/admin/setup-status");
    if (!status.initial_db_setup_seen) {
      openWizard();
    }
  } catch (_) {
    // 조회 자체가 실패해도 마법사를 못 띄울 뿐 앱 사용은 계속할 수 있어야 하므로 조용히 무시.
  }
}

function openWizard() {
  state.wizardActive = true;
  state.wizardPendingReturn = null;
  document.getElementById("wizard-password-form").reset();
  document.getElementById("wizard-password-error").hidden = true;
  showWizardStep(1);
  document.getElementById("initial-db-setup-modal").hidden = false;
}

/** 1~3 중 하나를 받아 해당 단계만 보여주고 나머지는 숨긴다. */
function showWizardStep(step) {
  document.getElementById("wizard-step-indicator").textContent = `${step} / 3단계`;
  [1, 2, 3].forEach((n) => {
    document.getElementById(`wizard-step-${n}`).hidden = n !== step;
  });
}

/** [v4.9] 로컬/외부 DB 설정 팝업(마법사 2·3단계가 잠시 가리고 여는 하위 팝업)이 닫혔을 때
 * 호출한다 - 마법사가 진행 중이 아니면(하위 팝업을 "데이터베이스" 탭에서 독립적으로 열었던
 * 경우) 아무 것도 하지 않는다. */
function wizardReturnFromSubModal() {
  if (!state.wizardActive) return;
  const next = state.wizardPendingReturn;
  state.wizardPendingReturn = null;
  if (next === "finish") {
    finishWizard();
  } else {
    document.getElementById("initial-db-setup-modal").hidden = false;
    showWizardStep(next || 1);
  }
}

async function finishWizard() {
  state.wizardActive = false;
  state.wizardPendingReturn = null;
  document.getElementById("initial-db-setup-modal").hidden = true;
  await markInitialDbSetupSeen();
  showToast('초기 설정을 마쳤습니다. 나중에 "데이터베이스" 메뉴에서 언제든 다시 설정할 수 있습니다.');
}

async function markInitialDbSetupSeen() {
  try {
    await api("/admin/setup-status/mark-seen", { method: "POST" });
  } catch (_) {
    /* no-op - 다음 로그인 때 마법사가 다시 뜨는 정도의 사소한 영향만 있음 */
  }
}

/** 마법사 1단계 - 관리자 로그인 비밀번호 변경 (건너뛰기 가능). 기존 "비밀번호 변경" 팝업과
 * 같은 PUT /auth/me/password를 재사용하되, id가 겹치지 않도록 wizard-* 필드를 따로 둔다. */
async function onWizardPasswordSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("wizard-password-error");
  errEl.hidden = true;
  const currentPassword = document.getElementById("wizard-password-current").value;
  const newPassword = document.getElementById("wizard-password-new").value;
  const confirmPassword = document.getElementById("wizard-password-new-confirm").value;
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
    showToast("비밀번호가 변경되었습니다.", "success");
    showWizardStep(2);
  } catch (err) {
    errEl.textContent = err.message || "변경에 실패했습니다.";
    errEl.hidden = false;
  }
}

/* ---------------------------- [v4.8] 로컬 DB 설정 (비밀번호 변경 / 전체 초기화) ---------------------------- */

/** "데이터베이스" 탭의 "현재 DB 상태" 팝업, 그리고 초기 설정 마법사 2단계가 공용으로 여는
 * 팝업 - 언제든 다시 비밀번호를 바꾸거나 초기화할 수 있다. */
function openLocalDbSetupModal() {
  document.getElementById("local-db-setup-form").reset();
  document.getElementById("local-db-setup-form").hidden = false;
  document.getElementById("local-db-setup-result").hidden = true;
  document.getElementById("local-db-setup-error").hidden = true;
  document.getElementById("local-db-setup-modal").hidden = false;
  loadLocalDbSetupEngineInfo();
}

function closeLocalDbSetupModal() {
  document.getElementById("local-db-setup-modal").hidden = true;
  wizardReturnFromSubModal();
}

/** 엔진이 SQLite면 "DB 계정 비밀번호"라는 개념 자체가 없으므로(파일 하나가 DB 전체),
 * 비밀번호 입력란을 숨기고 전체 초기화만 가능하도록 안내와 체크박스를 고정한다. */
async function loadLocalDbSetupEngineInfo() {
  let isSqlite = false;
  try {
    const data = await api("/admin/database");
    isSqlite = data.engine === "sqlite";
  } catch (_) {
    // 조회 실패 시에는 PostgreSQL로 가정(폼 그대로 노출) - 실제 적용 시점에 서버가 다시 검증한다.
  }
  state.localDbSetupIsSqlite = isSqlite;

  document.getElementById("local-db-setup-sqlite-note").hidden = !isSqlite;
  document.getElementById("local-db-setup-password-row").hidden = isSqlite;
  document.getElementById("local-db-setup-password-confirm-row").hidden = isSqlite;
  document.getElementById("local-db-setup-password").required = !isSqlite;
  document.getElementById("local-db-setup-password-confirm").required = !isSqlite;

  const wipeCheckbox = document.getElementById("local-db-setup-wipe");
  wipeCheckbox.disabled = isSqlite;
  if (isSqlite) wipeCheckbox.checked = true; // SQLite에서 유일하게 의미 있는 경로이므로 미리 켜둔다
}

async function onLocalDbSetupFormSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("local-db-setup-error");
  errEl.hidden = true;

  const isSqlite = !!state.localDbSetupIsSqlite;
  const wipe = document.getElementById("local-db-setup-wipe").checked;
  const password = document.getElementById("local-db-setup-password").value;
  const passwordConfirm = document.getElementById("local-db-setup-password-confirm").value;

  if (isSqlite && !wipe) {
    errEl.textContent = "SQLite 사용 중에는 전체 초기화 옵션만 선택할 수 있습니다.";
    errEl.hidden = false;
    return;
  }
  if (!isSqlite) {
    if (password.length < 8) {
      errEl.textContent = "비밀번호는 8자 이상이어야 합니다.";
      errEl.hidden = false;
      return;
    }
    if (password !== passwordConfirm) {
      errEl.textContent = "비밀번호가 서로 일치하지 않습니다.";
      errEl.hidden = false;
      return;
    }
  }
  if (wipe) {
    const confirmed = confirm(
      "기존에 수집된 VM/요금 데이터를 포함해 데이터베이스 전체를 초기화합니다. 이 작업은 되돌릴 " +
        "수 없고, 완료 즉시 현재 로그인 세션이 무효화되어 기본 관리자 계정(admin/admin1!2@3#)으로 " +
        "다시 로그인해야 합니다. 계속하시겠습니까?"
    );
    if (!confirmed) return;
  }

  const btn = document.getElementById("local-db-setup-submit-btn");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "적용 중...";
  try {
    const payload = {
      confirm: true,
      // SQLite 전체 초기화 경로는 실제로 쓰이지 않는 값이지만, 스키마가 항상 8자 이상을
      // 요구하므로(PostgreSQL 경로와 검증 로직을 공유) 자리표시용 값을 채워 보낸다.
      new_password: isSqlite ? "sqlite-wipe-placeholder" : password,
      wipe_data: wipe,
      confirm_wipe: wipe,
    };
    const result = await api("/admin/database/local-setup", { method: "POST", body: JSON.stringify(payload) });

    document.getElementById("local-db-setup-message").textContent = result.message;
    document.getElementById("local-db-setup-steps").innerHTML = result.next_steps
      .map((step) => `<li>${escapeHtml(step)}</li>`)
      .join("");
    document.getElementById("local-db-setup-result").hidden = false;
    document.getElementById("local-db-setup-form").hidden = true;
    showToast(result.wiped ? "로컬 DB를 초기화했습니다." : "DB 계정 비밀번호를 변경했습니다.", "success");

    if (result.wiped) {
      // 방금 로그인해 있던 admin 계정 행 자체가 삭제/재생성되었으므로, 현재 세션은 더 이상
      // 유효하지 않은 것으로 취급하고 자동으로 로그아웃시켜 기본 계정으로 재로그인을 유도한다.
      setTimeout(() => {
        closeLocalDbSetupModal();
        doLogout();
        showToast("초기화가 완료되어 로그아웃되었습니다. 기본 관리자 계정으로 다시 로그인하세요.");
      }, 2500);
    }
  } catch (err) {
    errEl.textContent = err.message || "적용에 실패했습니다.";
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function loadDatabaseOverview() {
  let data;
  try {
    data = await api("/admin/database");
  } catch (err) {
    showToast(err.message, "error");
    return;
  }

  const connLabel =
    data.engine === "sqlite"
      ? `파일: ${data.file_path || "-"}`
      : `${data.host}:${data.port} / DB: ${data.database} / 사용자: ${data.username}`;

  renderKpiGrid(document.getElementById("database-current-kpis"), [
    { label: "엔진", value: data.engine === "postgresql" ? "PostgreSQL" : "SQLite", sub: data.server_version || "", highlight: true },
    { label: "전체 크기", value: fmtBytes(data.size_bytes), sub: "" },
    { label: "연결 정보", value: connLabel, sub: "" },
  ]);

  const tbody = document.getElementById("database-table-body");
  tbody.innerHTML = data.tables
    .map(
      (t) => `
    <tr>
      <td><code>${escapeHtml(t.name)}</code></td>
      <td>${fmtNum(t.row_count)}</td>
      <td>${fmtBytes(t.size_bytes)}</td>
    </tr>`
    )
    .join("");
}

async function onDatabaseExport() {
  const btn = document.getElementById("database-export-btn");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "내보내는 중...";
  try {
    // downloadPdf()는 이름과 달리 인증 헤더를 붙여 파일을 받아 다운로드시키는 범용
    // 헬퍼라(v3 이후 PDF 결산서 전용으로 쓰여왔음), DB 백업 파일에도 그대로 재사용한다.
    await downloadPdf("/admin/database/export", "vcf-billing-backup");
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

/* ---------------------------- [v4.9] 관리자 화면 - Operations 데이터베이스 ---------------------------- */

function openOperationsDatabaseStatusModal() {
  document.getElementById("operations-database-status-modal").hidden = false;
  loadOperationsDatabaseOverview();
}

function closeOperationsDatabaseStatusModal() {
  document.getElementById("operations-database-status-modal").hidden = true;
}

/** GET /admin/database(Billing DB)의 Operations DB 버전 - loadDatabaseOverview()와
 * 동일한 렌더링을 operations-database-* id들에 채운다. */
async function loadOperationsDatabaseOverview() {
  let data;
  try {
    data = await api("/admin/operations-database");
  } catch (err) {
    showToast(err.message, "error");
    return;
  }

  const connLabel =
    data.engine === "sqlite"
      ? `파일: ${data.file_path || "-"}`
      : `${data.host}:${data.port} / DB: ${data.database} / 사용자: ${data.username}`;

  renderKpiGrid(document.getElementById("operations-database-current-kpis"), [
    { label: "엔진", value: data.engine === "postgresql" ? "PostgreSQL" : "SQLite", sub: data.server_version || "", highlight: true },
    { label: "전체 크기", value: fmtBytes(data.size_bytes), sub: "" },
    { label: "연결 정보", value: connLabel, sub: "" },
  ]);

  const tbody = document.getElementById("operations-database-table-body");
  tbody.innerHTML = data.tables
    .map(
      (t) => `
    <tr>
      <td><code>${escapeHtml(t.name)}</code></td>
      <td>${fmtNum(t.row_count)}</td>
      <td>${fmtBytes(t.size_bytes)}</td>
    </tr>`
    )
    .join("");
}

/** "데이터베이스" 탭의 "Operations 데이터베이스" 섹션과 초기 설정 마법사 3단계가 공용으로
 * 여는 팝업 - POST /admin/operations-database/local-setup으로 Billing DB와 같은
 * PostgreSQL 서버에 Operations 전용 데이터베이스를 새로 만든다. */
function openOperationsLocalDbModal() {
  document.getElementById("operations-local-db-form").reset();
  document.getElementById("operations-local-db-form").hidden = false;
  document.getElementById("operations-local-db-result").hidden = true;
  document.getElementById("operations-local-db-error").hidden = true;
  document.getElementById("operations-local-db-modal").hidden = false;
}

function closeOperationsLocalDbModal() {
  document.getElementById("operations-local-db-modal").hidden = true;
  wizardReturnFromSubModal();
}

async function onOperationsLocalDbFormSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("operations-local-db-error");
  errEl.hidden = true;
  const dbName = document.getElementById("operations-local-db-name").value.trim();

  const btn = document.getElementById("operations-local-db-submit-btn");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "생성 중...";
  try {
    const payload = { confirm: true };
    if (dbName) payload.db_name = dbName;
    const result = await api("/admin/operations-database/local-setup", { method: "POST", body: JSON.stringify(payload) });

    document.getElementById("operations-local-db-message").textContent = result.message;
    document.getElementById("operations-local-db-steps").innerHTML = result.next_steps
      .map((step) => `<li>${escapeHtml(step)}</li>`)
      .join("");
    document.getElementById("operations-local-db-result").hidden = false;
    document.getElementById("operations-local-db-form").hidden = true;
    showToast("Operations DB용 로컬 추가 데이터베이스를 생성했습니다.", "success");
  } catch (err) {
    errEl.textContent = err.message || "생성에 실패했습니다.";
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function _collectExternalDbForm() {
  return {
    host: document.getElementById("external-db-host").value.trim(),
    port: Number(document.getElementById("external-db-port").value) || 5432,
    database: document.getElementById("external-db-database").value.trim(),
    username: document.getElementById("external-db-username").value.trim(),
    password: document.getElementById("external-db-password").value,
    sslmode: document.getElementById("external-db-sslmode").value,
  };
}

async function onExternalDbTest() {
  const resultEl = document.getElementById("external-db-test-result");
  const errEl = document.getElementById("external-db-form-error");
  errEl.hidden = true;
  const btn = document.getElementById("external-db-test-btn");
  btn.disabled = true;
  resultEl.hidden = true;
  try {
    const result = await api("/admin/database/test-connection", {
      method: "POST",
      body: JSON.stringify(_collectExternalDbForm()),
    });
    resultEl.className = result.ok ? "form-note success" : "form-note error";
    resultEl.textContent = result.ok ? `✓ 연결 성공 (${result.server_version || ""})` : `✗ 연결 실패: ${result.message}`;
    resultEl.hidden = false;
  } catch (err) {
    errEl.textContent = err.message || "연결 테스트에 실패했습니다.";
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

async function onExternalDbMigrateSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("external-db-form-error");
  errEl.hidden = true;

  // [v4.9] state.externalDbTarget에 따라 Billing DB(/admin/database/migrate)와
  // Operations DB(/admin/operations-database/external-setup) 중 어느 쪽을 마이그레이션할지 정한다.
  const isOps = state.externalDbTarget === "operations";
  const dataLabel = isOps ? "Operations 데이터(VM 인벤토리/전원상태)" : "현재 데이터";

  if (
    !confirm(
      `대상 PostgreSQL DB로 ${dataLabel} 전체를 복사합니다. 대상 DB는 반드시 비어 있어야 하며, ` +
        "이 작업만으로는 앱이 실제로 그 DB를 쓰도록 전환되지 않습니다(안내에 따라 별도로 .env를 " +
        "바꾸고 컨테이너를 재시작해야 합니다). 계속하시겠습니까?"
    )
  ) {
    return;
  }

  const btn = document.getElementById("external-db-migrate-btn");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "마이그레이션 중...";
  try {
    const payload = { ..._collectExternalDbForm(), confirm: true };
    const endpoint = isOps ? "/admin/operations-database/external-setup" : "/admin/database/migrate";
    const result = await api(endpoint, { method: "POST", body: JSON.stringify(payload) });

    document.getElementById("external-db-migrate-message").textContent = result.message;
    document.getElementById("external-db-migrate-table-body").innerHTML = result.tables
      .map((t) => `<tr><td><code>${escapeHtml(t.name)}</code></td><td>${fmtNum(t.rows)}</td></tr>`)
      .join("");
    document.getElementById("external-db-migrate-url").value = result.database_url || "";
    document.getElementById("external-db-migrate-steps").innerHTML = result.next_steps
      .map((step) => `<li>${escapeHtml(step)}</li>`)
      .join("");
    document.getElementById("external-db-migrate-result").hidden = false;
    showToast("마이그레이션이 완료되었습니다.", "success");
  } catch (err) {
    errEl.textContent = err.message || "마이그레이션에 실패했습니다.";
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function onCopyExternalDbUrl() {
  const input = document.getElementById("external-db-migrate-url");
  try {
    await navigator.clipboard.writeText(input.value);
    showToast("클립보드에 복사했습니다.", "success");
  } catch (_) {
    input.select();
    showToast("자동 복사에 실패했습니다 - 직접 선택해 복사해주세요.", "error");
  }
}

/* ---------------------------- 관리자 화면 - 사용자 관리 (v4.5) ---------------------------- */

async function loadUserManagement() {
  try {
    const users = await api("/admin/users");
    state.users = users;
    renderUserTable(users);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function renderUserTable(users) {
  const tbody = document.getElementById("user-table-body");
  if (!users.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">등록된 사용자가 없습니다.</td></tr>`;
    return;
  }
  tbody.innerHTML = users
    .map(
      (u) => `
    <tr>
      <td>${escapeHtml(u.email)}</td>
      <td>${escapeHtml(u.display_name) || "-"}</td>
      <td>${u.role === "admin" ? "관리자" : "일반 사용자"}</td>
      <td>${u.role === "admin" ? "-" : escapeHtml(u.tenant_name) || `<span class="muted">미배정</span>`}</td>
      <td>
        <div class="row-actions">
          <button class="btn btn-ghost btn-sm user-reset-btn" data-user-id="${u.id}">비밀번호 초기화</button>
          <button class="btn btn-ghost btn-sm user-edit-btn" data-user-id="${u.id}">수정</button>
          <button class="btn btn-ghost btn-sm danger user-delete-btn" data-user-id="${u.id}">삭제</button>
        </div>
      </td>
    </tr>`
    )
    .join("");

  tbody.querySelectorAll(".user-reset-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const u = state.users.find((x) => x.id === Number(btn.dataset.userId));
      if (u) openResetPasswordModal(u);
    });
  });
  tbody.querySelectorAll(".user-edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const u = state.users.find((x) => x.id === Number(btn.dataset.userId));
      if (u) openUserModal("edit", u);
    });
  });
  tbody.querySelectorAll(".user-delete-btn").forEach((btn) => {
    btn.addEventListener("click", () => onUserDelete(Number(btn.dataset.userId)));
  });
}

// [v4.5] 생성/수정 모달의 "소속 테넌트" 드롭다운을 state.tenants(테넌트 관리 탭과 공유하는
// 캐시)로 채운다 - 관리자가 "테넌트 관리" 탭을 먼저 들르지 않고 "사용자 관리"로 바로 오는
// 경로도 있어(switchAdminPage), 그 경우엔 loadTenantManagement()를 먼저 호출해둔다.
function populateUserTenantSelect(selectedTenantId) {
  const select = document.getElementById("user-tenant-select");
  select.innerHTML = state.tenants.length
    ? state.tenants.map((t) => `<option value="${t.id}">${escapeHtml(t.name)} (${escapeHtml(t.key)})</option>`).join("")
    : `<option value="">등록된 테넌트가 없습니다 - 먼저 테넌트를 만드세요</option>`;
  if (selectedTenantId != null) select.value = String(selectedTenantId);
}

function updateUserTenantFieldVisibility() {
  const isUserRole = document.getElementById("user-role-select").value === "user";
  document.getElementById("user-tenant-row").hidden = !isUserRole;
}

function openUserModal(mode, user) {
  state.userModalMode = mode;
  state.editingUserId = user ? user.id : null;
  document.getElementById("user-form").reset();
  document.getElementById("user-form-error").hidden = true;
  populateUserTenantSelect(user ? user.tenant_id : null);

  const emailInput = document.getElementById("user-email-input");
  const roleSelect = document.getElementById("user-role-select");
  const passwordInput = document.getElementById("user-password-input");

  if (mode === "create") {
    document.getElementById("user-modal-title").textContent = "새 사용자";
    document.getElementById("user-form-submit-btn").textContent = "생성";
    emailInput.value = "";
    emailInput.disabled = false;
    roleSelect.value = "user";
    roleSelect.disabled = false;
    passwordInput.value = "";
    passwordInput.required = true;
    document.getElementById("user-password-row").hidden = false;
    document.getElementById("user-password-hint").hidden = true;
    document.getElementById("user-role-hint").hidden = true;
    document.getElementById("user-name-input").value = "";
  } else {
    document.getElementById("user-modal-title").textContent = `사용자 수정 — ${user.email}`;
    document.getElementById("user-form-submit-btn").textContent = "저장";
    emailInput.value = user.email;
    emailInput.disabled = true; // 로그인 id는 생성 후 불변으로 취급 (프로젝트 Key와 동일한 원칙)
    roleSelect.value = user.role;
    roleSelect.disabled = true; // 역할 전환은 이 화면 범위 밖 - user-role-hint 참고
    passwordInput.required = false;
    document.getElementById("user-password-row").hidden = true;
    document.getElementById("user-password-hint").hidden = false;
    document.getElementById("user-role-hint").hidden = false;
    document.getElementById("user-name-input").value = user.display_name || "";
  }
  updateUserTenantFieldVisibility();
  document.getElementById("user-modal").hidden = false;
}

function closeUserModal() {
  document.getElementById("user-modal").hidden = true;
}

async function onUserFormSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("user-form-error");
  errEl.hidden = true;
  const role = document.getElementById("user-role-select").value;
  const tenantId = document.getElementById("user-tenant-select").value;

  if (role === "user" && !tenantId) {
    errEl.textContent = "소속 테넌트를 선택하세요.";
    errEl.hidden = false;
    return;
  }

  if (state.userModalMode === "create") {
    const payload = {
      email: document.getElementById("user-email-input").value.trim(),
      password: document.getElementById("user-password-input").value,
      display_name: document.getElementById("user-name-input").value.trim(),
      role,
      tenant_id: role === "user" ? Number(tenantId) : null,
    };
    try {
      await api("/admin/users", { method: "POST", body: JSON.stringify(payload) });
      showToast("사용자 계정이 생성되었습니다.", "success");
      closeUserModal();
      await loadUserManagement();
    } catch (err) {
      errEl.textContent = err.message || "생성에 실패했습니다.";
      errEl.hidden = false;
    }
  } else {
    const payload = {
      display_name: document.getElementById("user-name-input").value.trim(),
      tenant_id: role === "user" ? Number(tenantId) : null,
    };
    try {
      await api(`/admin/users/${state.editingUserId}`, { method: "PUT", body: JSON.stringify(payload) });
      showToast("사용자 정보를 수정했습니다.", "success");
      closeUserModal();
      await loadUserManagement();
      // 테넌트 상세 모달의 읽기 전용 사용자 목록이 열려 있었다면 함께 갱신.
      if (state.currentTenantDetailId) await refreshTenantDetailAndList();
    } catch (err) {
      errEl.textContent = err.message || "수정에 실패했습니다.";
      errEl.hidden = false;
    }
  }
}

async function onUserDelete(userId) {
  if (!confirm("이 사용자 계정을 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다.")) return;
  try {
    await api(`/admin/users/${userId}`, { method: "DELETE" });
    showToast("사용자 계정을 삭제했습니다.", "success");
    await loadUserManagement();
    if (state.currentTenantDetailId) await refreshTenantDetailAndList();
  } catch (err) {
    showToast(err.message, "error");
  }
}

function openResetPasswordModal(user) {
  state.resettingUserId = user.id;
  document.getElementById("user-reset-password-target").textContent = user.email;
  document.getElementById("user-reset-password-form").reset();
  document.getElementById("user-reset-password-form-error").hidden = true;
  document.getElementById("user-reset-password-modal").hidden = false;
}

function closeResetPasswordModal() {
  document.getElementById("user-reset-password-modal").hidden = true;
}

async function onResetPasswordFormSubmit(e) {
  e.preventDefault();
  const errEl = document.getElementById("user-reset-password-form-error");
  const pw = document.getElementById("user-reset-password-new").value;
  const confirmPw = document.getElementById("user-reset-password-confirm").value;
  if (pw !== confirmPw) {
    errEl.textContent = "새 비밀번호가 일치하지 않습니다.";
    errEl.hidden = false;
    return;
  }
  try {
    await api(`/admin/users/${state.resettingUserId}/password`, { method: "PUT", body: JSON.stringify({ password: pw }) });
    showToast("비밀번호를 초기화했습니다.", "success");
    closeResetPasswordModal();
  } catch (err) {
    errEl.textContent = err.message || "초기화에 실패했습니다.";
    errEl.hidden = false;
  }
}

/* ---------------------------- 관리자 화면 - 개요 ---------------------------- */

// loadUserView와 동일한 이유의 요청 순번 가드 (예: 초기 30d 응답이 늦게 도착해
// 그 사이 사용자가 연 드릴다운/월 조회 화면을 덮어쓰는 것을 방지).
let adminViewRequestSeq = 0;

async function loadAdminView() {
  const mySeq = ++adminViewRequestSeq;
  const tenantQuery = state.adminTenantFilter ? `&tenant_id=${encodeURIComponent(state.adminTenantFilter)}` : "";
  const forecastQuery = state.adminTenantFilter ? `?tenant_id=${encodeURIComponent(state.adminTenantFilter)}` : "";
  let data;
  let forecast = null;
  try {
    // [v4.7] "이번 달 예상 청구액"은 선택된 조회 기간과 무관하게 항상 필요하므로 함께 가져온다
    // (현재 테넌트 필터는 그대로 반영). forecast 실패는 개요 화면 전체를 막지 않는다.
    const [overviewData, forecastData] = await Promise.all([
      api(`/admin/overview?${periodQueryString()}${tenantQuery}`),
      api(`/admin/forecast${forecastQuery}`).catch(() => null),
    ]);
    data = overviewData;
    forecast = forecastData;
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

  const currency = data.projects[0]?.currency || "KRW";
  renderKpiGrid(document.getElementById("admin-kpis"), [
    { label: "프로젝트 수", value: fmtNum(data.total_projects), dot: COLORS.vcpu },
    { label: "전체 VM", value: fmtNum(data.total_vms), sub: `가동 ${fmtNum(data.total_powered_on_vms)}대`, dot: COLORS.vmem },
    { label: "전체 기간 요금", value: fmtMoney(data.total_cost, currency), highlight: true },
    periodChangeKpi(data, currency),
    forecastKpi(forecast),
  ]);

  renderProjectComparisonChart("admin-project-chart", data.projects);
  renderAggregatedDailyChart("admin-daily-chart", data.projects);
  renderAdminProjectTable(data.projects);
  renderTenantSummary(data.tenant_id, data.tenant_summaries || []);
  renderDownsizingTable(
    document.getElementById("admin-downsizing-table-body"),
    document.getElementById("admin-downsizing-note"),
    computeDownsizingCandidates(data.projects),
    true
  );

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

  drilldownPaging.admin = { vms: project.vms, currency: project.currency, query: "", page: 1 };
  document.getElementById("drilldown-search").value = "";
  renderVmTablePaged("admin");

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
