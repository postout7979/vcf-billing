"""관리자 화면용 API.

- 사용량/요금 조회: 전체(또는 특정 테넌트) 프로젝트 사용량/요금 조회, 프로젝트별 단가(RateCard) 설정
- 계정 연동: VCF Operations/Aria Operations 연동 계정을 독립적으로 등록/수정/삭제하고,
  해당 계정이 수집한 vCenter/Datacenter/Cluster/VM Folder/VM Tag 인벤토리를 조회
- 테넌트 관리: 테넌트 생성/조회/수정/삭제, 테넌트 하위 프로젝트(Cluster/Folder/Tag 다중 선택
  기준) 생성/수정/삭제, 테넌트에 사용자 계정 생성/배정

[v4.9] Billing DB(Tenant/Project/RateCard/User)와 Operations DB(IntegrationAccount~
VirtualMachine/PowerSample)가 분리되면서, 두 데이터베이스를 함께 참조하는 대부분의
엔드포인트는 db(Depends(get_db))와 ops_db(Depends(get_ops_db)) 세션을 모두 받는다.
Project<->Cluster/Folder/Tag 매칭 기준은 더 이상 ORM relationship이 아니라
app/project_criteria.py(연결 테이블 id 목록)와 app/ops_queries.py(Operations DB 조회)로
조합한다.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.backup_restore import (
    BackupFormatError,
    RestoreConfirmationError,
    backup_filename,
    build_backup_manifest,
    export_backup_bundle,
    parse_backup_bundle,
    restore_backup_bundle,
)
from app.billing.aggregator import (
    available_months,
    calendar_month_period,
    compute_all_projects_usage,
    compute_month_forecast,
    compute_project_usage,
    default_period,
    period_over_period_change,
)
from app.billing.statement_pdf import build_project_statement_pdf
from app.collector import recompute_project_assignments, sync_account_once
from app.database import get_db, get_ops_db
from app.db_admin import (
    ExternalDbNotEmptyError,
    LocalDbSetupError,
    OperationsDbSetupError,
    apply_local_db_setup,
    apply_operations_local_setup,
    export_database_bytes,
    get_database_overview,
    get_setup_status,
    mark_setup_seen,
    migrate_to_external_postgres,
    operations_target,
    test_external_connection,
)
from app.models import Project, RateCard, RateCardHistory, Tenant, User, UserRole
from app.models_ops import Cluster, IntegrationAccount, IntegrationKind, Tag, VMFolder
from app.ops_queries import unassign_project_vms, unassign_projects_vms
from app.project_criteria import delete_project_criteria, get_project_criteria_ids, set_project_criteria
from app.routers.common import parse_month_param, parse_period, project_to_out, project_usage_to_schema
from app.schemas import (
    AdminOverviewOut,
    BackupManifestOut,
    BackupRestoreResult,
    BackupTableCountsOut,
    ClusterOut,
    DatabaseOverviewOut,
    DatacenterOut,
    ExternalDbConnectionRequest,
    ExternalDbMigrateRequest,
    ExternalDbMigrateResult,
    ExternalDbTestResult,
    IntegrationAccountCreate,
    IntegrationAccountOut,
    IntegrationAccountUpdate,
    IntegrationInventoryOut,
    IntegrationSyncOut,
    LocalDbSetupRequest,
    LocalDbSetupResult,
    MonthForecastOut,
    OperationsDbLocalSetupRequest,
    OperationsDbLocalSetupResult,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ProjectUsageOut,
    RateCardUpdate,
    SetupStatusOut,
    SystemStatusOut,
    TagOut,
    TenantCreate,
    TenantDetailOut,
    TenantOut,
    TenantSummaryOut,
    TenantUpdate,
    UserAdminOut,
    UserAdminUpdate,
    UserCreate,
    UserPasswordUpdate,
    VCenterOut,
    VMFolderOut,
)
from app.security.crypto import encrypt_secret
from app.system_status import get_system_status

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ==========================================================================
# [v4.4] 시스템 상태 (DB 사용량 / API 프로세스 리소스 / 수집기 동작 현황)
# ==========================================================================


@router.get("/system-status", response_model=SystemStatusOut)
def system_status(
    _admin: User = Depends(require_admin), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> SystemStatusOut:
    return get_system_status(db, ops_db)


# ==========================================================================
# [v4.6] 데이터베이스 (현재 Billing DB 정보 / 외부 PostgreSQL 연결 테스트·마이그레이션 / 내보내기)
# ==========================================================================


@router.get("/database", response_model=DatabaseOverviewOut)
def database_overview(_admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> DatabaseOverviewOut:
    """현재 Billing DB(Tenant/Project/RateCard/User 등)의 접속 정보/크기/테이블별 통계."""
    return get_database_overview(db)


@router.post("/database/test-connection", response_model=ExternalDbTestResult)
def database_test_connection(
    payload: ExternalDbConnectionRequest, _admin: User = Depends(require_admin)
) -> ExternalDbTestResult:
    return test_external_connection(payload)


@router.post("/database/migrate", response_model=ExternalDbMigrateResult)
def database_migrate(
    payload: ExternalDbMigrateRequest, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> ExternalDbMigrateResult:
    """Billing DB 전체를 외부 PostgreSQL로 마이그레이션한다 (Operations DB는 건드리지 않음 -
    Operations DB를 옮기려면 아래 POST /operations-database/external-setup을 사용하세요)."""
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confirm=true로 명시적으로 확인해야 마이그레이션이 실행됩니다")
    try:
        return migrate_to_external_postgres(db, payload)
    except ExternalDbNotEmptyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 접속/네트워크 등 다양한 원인을 그대로 안내
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"마이그레이션 실패: {exc}") from exc


@router.get("/database/export")
def database_export(_admin: User = Depends(require_admin)) -> Response:
    """Billing DB 전체를 백업 파일로 내려받는다 (Operations DB는 포함되지 않음)."""
    try:
        data, filename, media_type = export_database_bytes()
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc)) from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ==========================================================================
# [v4.9] Operations DB (VM 인벤토리/전원상태 수집 데이터) - "로컬 추가 데이터베이스" /
# "외부 데이터베이스" 설정 마법사가 호출하는 엔드포인트. Billing DB와 별개의 물리 DB로
# 분리할 수 있게 하는 것이 목적이며, 아래 세 엔드포인트 자체는 "wizard를 봤는지" 플래그를
# 건드리지 않는다(프론트엔드가 마법사 전체를 마친 뒤 POST /setup-status/mark-seen을
# 별도로 호출한다).
# ==========================================================================


@router.get("/operations-database", response_model=DatabaseOverviewOut)
def operations_database_overview(
    _admin: User = Depends(require_admin), ops_db: Session = Depends(get_ops_db)
) -> DatabaseOverviewOut:
    """현재 Operations DB(VirtualMachine/PowerSample/IntegrationAccount 등)의 접속
    정보/크기/테이블별 통계. GET /database(Billing DB)의 Operations DB 버전."""
    return get_database_overview(ops_db, target=operations_target())


@router.post("/operations-database/local-setup", response_model=OperationsDbLocalSetupResult)
def operations_database_local_setup(
    payload: OperationsDbLocalSetupRequest, _admin: User = Depends(require_admin)
) -> OperationsDbLocalSetupResult:
    """"로컬 추가 데이터베이스" 선택 - 현재 Billing DB와 같은 PostgreSQL 서버에 Operations
    전용 데이터베이스를 새로 생성한다 (Billing DB가 SQLite 폴백이면 대신 로컬 파일을 하나
    더 만든다). 응답의 operations_database_url을 .env의 OPERATIONS_DATABASE_URL에 반영하고
    재기동해야 실제로 전환된다 (next_steps 참고) - 이 호출 자체는 새 DB를 만들기만 할 뿐,
    지금 이 프로세스가 쓰는 연결을 바꾸지 않는다.
    """
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confirm=true로 명시적으로 확인해야 실행됩니다")
    try:
        return apply_operations_local_setup(payload)
    except OperationsDbSetupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - DB 접속/권한 등 다양한 원인을 그대로 안내
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"로컬 추가 데이터베이스 생성 실패: {exc}") from exc


@router.post("/operations-database/external-setup", response_model=ExternalDbMigrateResult)
def operations_database_external_setup(
    payload: ExternalDbMigrateRequest, _admin: User = Depends(require_admin), ops_db: Session = Depends(get_ops_db)
) -> ExternalDbMigrateResult:
    """"외부 데이터베이스" 선택 - 현재 Operations DB의 데이터를 관리자가 입력한 외부
    PostgreSQL로 마이그레이션한다 (POST /database/migrate의 Operations DB 버전, 대상
    metadata만 다르다). 대상 DB는 반드시 비어 있어야 한다."""
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confirm=true로 명시적으로 확인해야 마이그레이션이 실행됩니다")
    try:
        return migrate_to_external_postgres(ops_db, payload, target=operations_target())
    except ExternalDbNotEmptyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 접속/네트워크 등 다양한 원인을 그대로 안내
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"마이그레이션 실패: {exc}") from exc


# ==========================================================================
# [v4.10] 백업 & 복구 (Billing DB + Operations DB 통합, 신규 배포 시 테넌트/프로젝트/
# 과금 원천 데이터 손실 방지). 연동 계정 접속정보는 백업에 포함하지 않는다 - 복구 후
# "계정 연동" 화면에서 다시 입력해야 한다. app/backup_restore.py 상단 주석 참고.
# ==========================================================================


@router.get("/backup/manifest", response_model=BackupManifestOut)
def backup_manifest(
    _admin: User = Depends(require_admin), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> BackupManifestOut:
    """"백업 대상 리스트" 화면에 보여줄, 현재 시점 기준 테이블별 대상/행수 목록."""
    items = build_backup_manifest(db, ops_db)
    return BackupManifestOut(
        generated_at=dt.datetime.now(dt.timezone.utc),
        note=(
            "백업 파일은 VCF Operations 연동 계정의 접속정보(URL/계정명/암호화된 비밀번호)를 "
            "포함하지 않습니다 - 복구 후 '계정 연동' 화면에서 다시 입력해야 합니다."
        ),
        items=items,
    )


@router.get("/backup/export")
def backup_export(
    _admin: User = Depends(require_admin), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> Response:
    """Billing DB + Operations DB의 백업 대상 데이터를 하나의 파일(.json.gz)로 내려받는다."""
    data = export_backup_bundle(db, ops_db)
    return Response(
        content=data,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{backup_filename()}"'},
    )


@router.post("/backup/restore", response_model=BackupRestoreResult)
def backup_restore(
    file: UploadFile = File(..., description="backup/export로 내려받은 .json.gz 백업 파일"),
    confirm: bool = Form(False, description="true로 명시적으로 확인해야 실행됩니다 (파괴적 - 현재 데이터 전체 교체)"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> BackupRestoreResult:
    """백업 파일로부터 Billing DB + Operations DB를 복구한다.

    복구는 항상 파괴적 전체 교체다 - 대상 테이블(app/backup_restore.py의 BILLING_TABLES/
    OPERATIONS_TABLES)을 모두 비운 뒤 백업 내용으로 다시 채운다. "신규 배포 직후,
    아직 이 배포에는 의미 있는 데이터가 없는 상태에서 이전 백업을 되살리는 것"이
    주 사용 시나리오다 - 이미 운영 중인 배포에 실행하면 그 사이에 쌓인 데이터가 모두
    사라지므로, 프론트엔드가 confirm() 다이얼로그로 두 번 확인한 뒤에만 confirm=true로 보낸다.
    """
    if not confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confirm=true로 명시적으로 확인해야 복구가 실행됩니다")
    raw = file.file.read()
    try:
        bundle = parse_backup_bundle(raw)
        counts = restore_backup_bundle(db, ops_db, bundle, confirm=confirm)
    except BackupFormatError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except RestoreConfirmationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - DB 오류 등 다양한 원인을 그대로 안내
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"복구 실패: {exc}") from exc

    billing_rows = sum(counts["billing"].values())
    ops_rows = sum(counts["operations"].values())
    return BackupRestoreResult(
        ok=True,
        message=(
            f"복구를 완료했습니다 (Billing DB {billing_rows}행, Operations DB {ops_rows}행). "
            "접속정보가 제외된 연동 계정은 '계정 연동' 화면에서 재등록해야 정상 동작합니다."
        ),
        counts=BackupTableCountsOut(billing=counts["billing"], operations=counts["operations"]),
    )


# ==========================================================================
# [v4.8] 최초 로그인 DB 설정 게이트 (로컬 DB 계속 사용 / 외부 DB 연동 안내 + 로컬 DB
# 계정 비밀번호 변경/전체 초기화) - "데이터베이스" 개편 배경은 app/db_admin.py 상단
# 주석 및 claude/vcf-billing-portal-design.md "v4.8 개편" 참고. [v4.9] 이 세 엔드포인트
# 자체는 기존 계약 그대로(Billing DB 전용) 유지하고, Operations DB 선택지는 위 새
# 엔드포인트 세 개가 담당한다 - "wizard를 이미 봤는지" 플래그는 여전히 이 섹션의
# setup-status 하나로 마법사 전체를 대표한다.
# ==========================================================================


@router.get("/setup-status", response_model=SetupStatusOut)
def setup_status(_admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> SetupStatusOut:
    return SetupStatusOut(initial_db_setup_seen=get_setup_status(db))


@router.post("/setup-status/mark-seen", response_model=SetupStatusOut)
def setup_status_mark_seen(
    _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> SetupStatusOut:
    """최초 설정 게이트를 건너뛰거나(외부 DB 연동을 선택해 별도 화면으로 이동) 안내만 확인한
    경우 - 이후 로그인부터는 게이트가 다시 뜨지 않도록 표시만 한다."""
    mark_setup_seen(db)
    return SetupStatusOut(initial_db_setup_seen=True)


@router.post("/database/local-setup", response_model=LocalDbSetupResult)
def database_local_setup(
    payload: LocalDbSetupRequest, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> LocalDbSetupResult:
    """최초 설정 게이트에서 "로컬 DB 계속 사용"을 고른 뒤 제출하는 폼 - DB 계정 비밀번호
    변경(안전, 기본값)이나 전체 초기화+비밀번호 변경(파괴적, wipe_data=true) 중 관리자가
    직접 고른 쪽을 실행한다. (Billing DB 전용 - Operations DB는 건드리지 않는다.)"""
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "confirm=true로 명시적으로 확인해야 실행됩니다")
    if payload.wipe_data and not payload.confirm_wipe:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "전체 초기화는 confirm_wipe=true로 별도 확인해야 실행됩니다 (기존 VM/요금 데이터가 모두 삭제됩니다)",
        )
    try:
        return apply_local_db_setup(db, payload)
    except LocalDbSetupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - DB 접속/권한 등 다양한 원인을 그대로 안내
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"로컬 DB 설정 실패: {exc}") from exc


# ==========================================================================
# 사용량/요금 조회 (전체 또는 tenant_id 필터로 특정 테넌트 교차 확인)
# ==========================================================================


@router.get("/months", response_model=list[str])
def list_available_months(
    tenant_id: int | None = Query(None, description="지정 시 해당 테넌트 소속 데이터만 대상으로 함"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> list[str]:
    """데이터가 존재하는 캘린더 월 목록 (YYYY-MM, 최신순) - 월 선택 드롭다운을 채우는 데 사용."""
    return available_months(db, ops_db, tenant_id=tenant_id)


@router.get("/overview", response_model=AdminOverviewOut)
def overview(
    period: str = Query("30d", description="7d | 30d | mtd | month | custom"),
    start: str | None = None,
    end: str | None = None,
    month: str | None = Query(None, description="period=month 일 때 YYYY-MM 형식"),
    tenant_id: int | None = Query(None, description="지정 시 해당 테넌트만 조회 (교차 확인)"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> AdminOverviewOut:
    s, e = parse_period(period, start, end, month)
    results = compute_all_projects_usage(db, ops_db, s, e, tenant_id=tenant_id)
    project_schemas = [project_usage_to_schema(r, s, e) for r in results]

    currencies = {p.currency for p in project_schemas}
    if not project_schemas:
        currency_note = "등록된 프로젝트가 없습니다"
    elif len(currencies) == 1:
        currency_note = f"전체 {next(iter(currencies))} 기준 합산"
    else:
        currency_note = "프로젝트별 통화가 상이하여 합계는 참고용입니다"

    tenant_name = None
    if tenant_id is not None:
        tenant = db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "테넌트를 찾을 수 없습니다")
        tenant_name = tenant.name

    # [v4.2] "전체 테넌트 (교차 확인)" 화면에서 프로젝트 목록 위에 테넌트별 집계를
    # 보여주기 위한 그룹핑. 특정 테넌트로 필터링한 조회(tenant_id 지정)는 테넌트가
    # 하나뿐이라 만들 필요가 없다 - 항상 빈 리스트를 반환한다.
    tenant_summaries: list[TenantSummaryOut] = []
    if tenant_id is None:
        order: list[int] = []
        by_tenant: dict[int, dict] = {}
        for p in project_schemas:
            agg = by_tenant.get(p.tenant_id)
            if agg is None:
                agg = {
                    "tenant_key": p.tenant_key,
                    "tenant_name": p.tenant_name,
                    "project_count": 0,
                    "vm_count": 0,
                    "powered_on_vm_count": 0,
                    "total_cost": 0.0,
                    "currencies": set(),
                }
                by_tenant[p.tenant_id] = agg
                order.append(p.tenant_id)
            agg["project_count"] += 1
            agg["vm_count"] += p.vm_count
            agg["powered_on_vm_count"] += p.powered_on_vm_count
            agg["total_cost"] += p.total_cost
            agg["currencies"].add(p.currency)

        for tid in order:
            agg = by_tenant[tid]
            currency_note = next(iter(agg["currencies"])) if len(agg["currencies"]) == 1 else "혼합"
            tenant_summaries.append(
                TenantSummaryOut(
                    tenant_id=tid,
                    tenant_key=agg["tenant_key"],
                    tenant_name=agg["tenant_name"],
                    project_count=agg["project_count"],
                    vm_count=agg["vm_count"],
                    powered_on_vm_count=agg["powered_on_vm_count"],
                    total_cost=round(agg["total_cost"], 2),
                    currency_note=currency_note,
                )
            )
        tenant_summaries.sort(key=lambda t: t.tenant_name)

    total_cost = round(sum(p.total_cost for p in project_schemas), 2)
    previous_period_total_cost, period_over_period_change_pct = period_over_period_change(
        db, ops_db, s, e, total_cost, tenant_id=tenant_id
    )

    return AdminOverviewOut(
        period_start=s,
        period_end=e,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        total_projects=len(project_schemas),
        total_vms=sum(p.vm_count for p in project_schemas),
        total_powered_on_vms=sum(p.powered_on_vm_count for p in project_schemas),
        total_cost=total_cost,
        currency_note=currency_note,
        projects=project_schemas,
        tenant_summaries=tenant_summaries,
        previous_period_total_cost=previous_period_total_cost,
        period_over_period_change_pct=period_over_period_change_pct,
    )


@router.get("/forecast", response_model=MonthForecastOut)
def month_forecast(
    tenant_id: int | None = Query(None, description="지정 시 해당 테넌트만 대상으로 함"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> MonthForecastOut:
    """[v4.7] 이번 달(1일~현재) 실적을 바탕으로 한 이번 달 예상 청구액. 화면에서 선택 중인
    조회 기간(period)과 무관하게 항상 현재 캘린더 월 기준으로 계산한다."""
    result = compute_month_forecast(db, ops_db, tenant_id=tenant_id)
    return MonthForecastOut(
        month=result.month,
        mtd_total_cost=result.mtd_total_cost,
        days_elapsed=result.days_elapsed,
        days_in_month=result.days_in_month,
        forecast_total_cost=result.forecast_total_cost,
        currency_note=result.currency_note,
    )


@router.get("/projects/{project_id}/usage", response_model=ProjectUsageOut)
def project_usage(
    project_id: int,
    period: str = Query("30d", description="7d | 30d | mtd | month | custom"),
    start: str | None = None,
    end: str | None = None,
    month: str | None = Query(None, description="period=month 일 때 YYYY-MM 형식"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> ProjectUsageOut:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "프로젝트를 찾을 수 없습니다")
    s, e = parse_period(period, start, end, month)
    result = compute_project_usage(db, ops_db, project, s, e)
    return project_usage_to_schema(result, s, e)


@router.get("/projects/{project_id}/statement.pdf")
def project_statement_pdf(
    project_id: int,
    month: str = Query(..., description="YYYY-MM 형식 (예: 2026-08)"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> Response:
    """지정 프로젝트의 특정 캘린더 월 사용량 결산서를 PDF로 내려받는다 (관리자 - 전체 테넌트 대상)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "프로젝트를 찾을 수 없습니다")
    year, month_int = parse_month_param(month)
    s, e = calendar_month_period(year, month_int)
    result = compute_project_usage(db, ops_db, project, s, e)
    pdf_bytes = build_project_statement_pdf(result, year, month_int, s, e, tenant_name=project.tenant.name)
    filename = f"{project.key}_{year}-{month_int:02d}_statement.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.put("/projects/{project_id}/rates", response_model=ProjectUsageOut)
def update_rates(
    project_id: int,
    payload: RateCardUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> ProjectUsageOut:
    """프로젝트별 vCPU/vMEM/vDisk 시간당 단가를 설정한다. 변경 이력은 감사 로그로 남는다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "프로젝트를 찾을 수 없습니다")

    rate = project.rate_card
    if rate is None:
        rate = RateCard(project_id=project.id)
        db.add(rate)

    rate.vcpu_rate_per_hour = payload.vcpu_rate_per_hour
    rate.vmem_rate_per_hour_gb = payload.vmem_rate_per_hour_gb
    rate.vdisk_rate_per_hour_gb = payload.vdisk_rate_per_hour_gb
    rate.currency = payload.currency
    rate.usage_weight_enabled = payload.usage_weight_enabled
    rate.usage_weight_floor_pct = payload.usage_weight_floor_pct
    rate.updated_by = admin.email

    db.add(
        RateCardHistory(
            project_id=project.id,
            vcpu_rate_per_hour=payload.vcpu_rate_per_hour,
            vmem_rate_per_hour_gb=payload.vmem_rate_per_hour_gb,
            vdisk_rate_per_hour_gb=payload.vdisk_rate_per_hour_gb,
            currency=payload.currency,
            usage_weight_enabled=payload.usage_weight_enabled,
            usage_weight_floor_pct=payload.usage_weight_floor_pct,
            changed_by=admin.email,
        )
    )
    db.commit()

    s, e = default_period(30)
    result = compute_project_usage(db, ops_db, project, s, e)
    return project_usage_to_schema(result, s, e)


# ==========================================================================
# 계정 연동 (VCF Operations / Aria Operations) - 독립/전역 엔티티, Operations DB 소속
# ==========================================================================


def _get_account_or_404(ops_db: Session, account_id: int) -> IntegrationAccount:
    account = ops_db.get(IntegrationAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "연동 계정을 찾을 수 없습니다")
    return account


def _integration_account_to_out(a: IntegrationAccount) -> IntegrationAccountOut:
    return IntegrationAccountOut(
        id=a.id,
        kind=a.kind.value,
        name=a.name,
        base_url=a.base_url,
        username=a.username,
        auth_source=a.auth_source,
        verify_ssl=a.verify_ssl,
        is_mock=a.is_mock,
        vcenter_count=len(a.vcenters),
        vm_count=len(a.vms),
        updated_at=a.updated_at,
        updated_by=a.updated_by,
        last_sync_status=a.last_sync_status,
        last_sync_at=a.last_sync_at,
        last_sync_error=a.last_sync_error,
        last_sync_vm_count=a.last_sync_vm_count,
        needs_reconnect=a.needs_reconnect,
    )


def _cluster_to_out(c: Cluster) -> ClusterOut:
    return ClusterOut(id=c.id, external_id=c.external_id, name=c.name, vm_count=len(c.vms))


def _folder_to_out(f: VMFolder) -> VMFolderOut:
    return VMFolderOut(id=f.id, external_id=f.external_id, path=f.path, name=f.name, vm_count=len(f.vms))


def _tag_to_out(t: Tag) -> TagOut:
    return TagOut(id=t.id, category=t.category, name=t.name, label=t.label)


def _build_inventory_out(account: IntegrationAccount) -> IntegrationInventoryOut:
    vcenters = [
        VCenterOut(
            id=vc.id,
            external_id=vc.external_id,
            name=vc.name,
            datacenters=[
                DatacenterOut(
                    id=dc.id,
                    external_id=dc.external_id,
                    name=dc.name,
                    clusters=[_cluster_to_out(c) for c in sorted(dc.clusters, key=lambda c: c.name)],
                    folders=[_folder_to_out(f) for f in sorted(dc.folders, key=lambda f: f.path)],
                )
                for dc in sorted(vc.datacenters, key=lambda dc: dc.name)
            ],
        )
        for vc in sorted(account.vcenters, key=lambda vc: vc.name)
    ]
    tags = [_tag_to_out(t) for t in sorted(account.tags, key=lambda t: (t.category, t.name))]
    return IntegrationInventoryOut(
        integration_account_id=account.id,
        integration_account_name=account.name,
        vcenters=vcenters,
        tags=tags,
        vm_count=len(account.vms),
    )


@router.get("/integration-accounts", response_model=list[IntegrationAccountOut])
def list_integration_accounts(
    _admin: User = Depends(require_admin), ops_db: Session = Depends(get_ops_db)
) -> list[IntegrationAccountOut]:
    accounts = ops_db.query(IntegrationAccount).order_by(IntegrationAccount.name).all()
    return [_integration_account_to_out(a) for a in accounts]


@router.post("/integration-accounts", response_model=IntegrationAccountOut, status_code=status.HTTP_201_CREATED)
def create_integration_account(
    payload: IntegrationAccountCreate, admin: User = Depends(require_admin), ops_db: Session = Depends(get_ops_db)
) -> IntegrationAccountOut:
    """VCF Operations/Aria Operations 연동 계정을 독립적으로 등록한다 (특정 테넌트에 종속되지 않음).

    이 계정 하나로 수집한 인벤토리(vCenter~VM)를 여러 테넌트/프로젝트가 나눠서 사용할 수 있다.
    비밀번호는 앱 레벨로 암호화해서 저장되며, 응답에는 절대 포함되지 않는다.
    """
    try:
        kind = IntegrationKind(payload.kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'kind는 "vcf_ops"여야 합니다') from exc

    if ops_db.query(IntegrationAccount).filter_by(name=payload.name).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용 중인 연동 계정 이름입니다")

    account = IntegrationAccount(
        kind=kind,
        name=payload.name,
        base_url=payload.base_url.rstrip("/"),
        username=payload.username,
        password_encrypted=encrypt_secret(payload.password),
        auth_source=payload.auth_source,
        verify_ssl=payload.verify_ssl,
        updated_by=admin.email,
    )
    ops_db.add(account)
    ops_db.commit()
    ops_db.refresh(account)

    # 등록 즉시 연결을 시도해 인벤토리를 1회 가져온다 - 계정 정보가 맞는지 곧바로 확인할 수
    # 있게 하기 위함. 실패해도 계정 자체는 그대로 생성된 채 남고(상태만 "error"), 관리자가
    # "계정 연동" 화면에서 정보를 고쳐 저장하거나 "가져오기" 버튼으로 재시도할 수 있다.
    # sync_account_once()가 내부적으로 Billing DB 세션을 열어 Project 배정 재계산까지 한다.
    sync_account_once(ops_db, account)
    ops_db.refresh(account)
    return _integration_account_to_out(account)


@router.put("/integration-accounts/{account_id}", response_model=IntegrationAccountOut)
def update_integration_account(
    account_id: int,
    payload: IntegrationAccountUpdate,
    admin: User = Depends(require_admin),
    ops_db: Session = Depends(get_ops_db),
) -> IntegrationAccountOut:
    account = _get_account_or_404(ops_db, account_id)

    if payload.name is not None and payload.name != account.name:
        if ops_db.query(IntegrationAccount).filter(IntegrationAccount.name == payload.name, IntegrationAccount.id != account.id).one_or_none():
            raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용 중인 연동 계정 이름입니다")
        account.name = payload.name

    # 연결 정보(URL/계정/비밀번호/인증소스/SSL검증)가 바뀌면 연동 상태가 더 이상 유효하지
    # 않으므로, 커밋 후 자동으로 재연동을 시도해 상태 배지를 곧바로 최신화한다.
    connection_changed = False
    if payload.base_url is not None and payload.base_url.rstrip("/") != account.base_url:
        account.base_url = payload.base_url.rstrip("/")
        connection_changed = True
    if payload.username is not None and payload.username != account.username:
        account.username = payload.username
        connection_changed = True
    if payload.password:
        account.password_encrypted = encrypt_secret(payload.password)
        connection_changed = True
    if payload.auth_source is not None and payload.auth_source != account.auth_source:
        account.auth_source = payload.auth_source
        connection_changed = True
    if payload.verify_ssl is not None and payload.verify_ssl != account.verify_ssl:
        account.verify_ssl = payload.verify_ssl
        connection_changed = True
    # [v4.10] 백업 복구로 생성된 자리표시자 계정(needs_reconnect=True)에 실제 접속정보를
    # 입력해 저장하면 정상 계정으로 전환한다 - id가 그대로라 이미 복구되어 있던
    # VM/PowerSample 등은 별도 처리 없이 다음 수집부터 같은 행이 갱신(upsert)된다.
    if connection_changed and account.needs_reconnect:
        account.needs_reconnect = False
    account.updated_by = admin.email
    ops_db.commit()
    ops_db.refresh(account)

    if connection_changed:
        sync_account_once(ops_db, account)
        ops_db.refresh(account)
    return _integration_account_to_out(account)


@router.delete("/integration-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_integration_account(
    account_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> None:
    """연동 계정을 삭제한다. 이 계정이 수집한 인벤토리(vCenter~VM)도 함께 삭제되며, 이 인벤토리를
    매칭 기준으로 사용하던 Project는 더 이상 해당 VM을 표시하지 않게 된다.

    [v4.9] 삭제된 Cluster/VMFolder/Tag의 id가 project_cluster_link 등(Billing DB)에
    소프트 참조로 남아 있을 수 있으나, 그 id를 참조하는 VM 자체가 더 이상 없으므로
    recompute_project_assignments()의 매칭 결과에는 영향이 없다 (app/models.py 상단
    주석 참고) - 링크 테이블의 그 orphan 행 자체를 지우지는 않는다(굳이 지울 필요가
    없고, 관리자가 나중에 같은 external_id로 계정을 다시 연동하면 새 id로 재매칭됨).
    """
    account = _get_account_or_404(ops_db, account_id)
    ops_db.delete(account)
    ops_db.commit()
    recompute_project_assignments(db, ops_db)


@router.post("/integration-accounts/{account_id}/sync", response_model=IntegrationSyncOut)
def sync_integration_account(
    account_id: int, _admin: User = Depends(require_admin), ops_db: Session = Depends(get_ops_db)
) -> IntegrationSyncOut:
    """"가져오기" 버튼 - 5분 주기 자동 수집을 기다리지 않고 즉시 연결을 시도해 최신
    인벤토리(vCenter~VM, vCPU/vMEM/vDisk, Tag)를 가져온다. 연결 실패 시에도 200으로
    응답하며(status="error"), 실패 사유는 계정의 last_sync_error 및 이후 계정 목록
    조회에도 그대로 남아 상태 배지에 표시된다."""
    account = _get_account_or_404(ops_db, account_id)
    sync_status, message, new_samples = sync_account_once(ops_db, account)
    ops_db.refresh(account)
    return IntegrationSyncOut(
        account=_integration_account_to_out(account),
        status=sync_status,
        message=message,
        new_sample_count=new_samples,
    )


@router.get("/integration-accounts/{account_id}/inventory", response_model=IntegrationInventoryOut)
def get_integration_inventory(
    account_id: int, _admin: User = Depends(require_admin), ops_db: Session = Depends(get_ops_db)
) -> IntegrationInventoryOut:
    """연동 계정의 vCenter/Datacenter/Cluster/VM Folder/VM Tag 전체 계층을 조회한다.

    프로젝트 생성/수정 화면에서 Cluster/VM Folder/VM Tag 다중 선택 체크박스를 채우는 데 쓴다.
    아직 한 번도 수집되지 않은 계정은 빈 인벤토리를 반환한다 (수집기가 5분 주기로 자동 수집).
    """
    account = _get_account_or_404(ops_db, account_id)
    return _build_inventory_out(account)


# ==========================================================================
# 테넌트 관리
# ==========================================================================


def _tenant_to_out(t: Tenant) -> TenantOut:
    return TenantOut(
        id=t.id,
        key=t.key,
        name=t.name,
        description=t.description,
        project_count=len(t.projects),
        user_count=len(t.users),
        created_at=t.created_at,
    )


def _user_to_admin_out(u: User) -> UserAdminOut:
    return UserAdminOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        role=u.role.value,
        tenant_id=u.tenant_id,
        tenant_key=u.tenant.key if u.tenant else None,
        tenant_name=u.tenant.name if u.tenant else None,
    )


def _get_tenant_or_404(db: Session, tenant_id: int) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "테넌트를 찾을 수 없습니다")
    return tenant


def _get_tenant_project_or_404(db: Session, tenant: Tenant, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "프로젝트를 찾을 수 없습니다")
    return project


def _validate_criteria_ids(ops_db: Session, model, ids: list[int]) -> list[int]:
    """Cluster/VMFolder/Tag id 목록이 Operations DB에 실제로 존재하는지 검증하고
    중복을 제거해 반환한다. 존재하지 않는 id가 있으면 400.

    [v4.9] 예전에는 실제 ORM 행을 반환해 project.clusters = rows 처럼 relationship에
    바로 대입했지만, Project<->Cluster/Folder/Tag가 더 이상 relationship이 아니므로
    id만 검증해서 돌려주고 실제 저장은 app/project_criteria.set_project_criteria()가 한다.
    """
    if not ids:
        return []
    unique_ids = list(dict.fromkeys(ids))
    count = ops_db.query(model).filter(model.id.in_(unique_ids)).count()
    if count != len(unique_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"존재하지 않는 {model.__name__} id가 포함되어 있습니다")
    return unique_ids


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(_admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[TenantOut]:
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return [_tenant_to_out(t) for t in tenants]


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
def create_tenant(payload: TenantCreate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> TenantOut:
    if db.query(Tenant).filter_by(key=payload.key).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용 중인 테넌트 key입니다")

    tenant = Tenant(key=payload.key, name=payload.name, description=payload.description)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return _tenant_to_out(tenant)


@router.get("/tenants/{tenant_id}", response_model=TenantDetailOut)
def get_tenant(
    tenant_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> TenantDetailOut:
    tenant = _get_tenant_or_404(db, tenant_id)
    base = _tenant_to_out(tenant)
    return TenantDetailOut(
        **base.model_dump(),
        projects=[project_to_out(db, ops_db, p) for p in sorted(tenant.projects, key=lambda p: p.name)],
        users=[_user_to_admin_out(u) for u in sorted(tenant.users, key=lambda u: u.email)],
    )


@router.put("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: int, payload: TenantUpdate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> TenantOut:
    tenant = _get_tenant_or_404(db, tenant_id)
    if payload.name is not None:
        tenant.name = payload.name
    if payload.description is not None:
        tenant.description = payload.description
    db.commit()
    db.refresh(tenant)
    return _tenant_to_out(tenant)


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_tenant(
    tenant_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> None:
    """테넌트를 삭제한다. 하위 Project/User도 함께 삭제된다 (연동 계정/인벤토리는 삭제되지 않음 -
    독립 엔티티이므로 다른 테넌트가 계속 사용할 수 있다).

    [v4.9] 하위 Project가 배정해 뒀던 VM(Operations DB)의 project_id를 먼저 None으로
    되돌리고, 매칭 기준 연결 테이블(Billing DB)도 프로젝트별로 명시적으로 정리한다 -
    Project<->Cluster/Folder/Tag가 더 이상 relationship이 아니라서 Tenant.projects의
    cascade="all, delete-orphan"만으로는 링크 테이블 행까지 자동으로 지워지지 않는다.
    """
    tenant = _get_tenant_or_404(db, tenant_id)
    project_ids = [p.id for p in tenant.projects]
    if project_ids:
        unassign_projects_vms(ops_db, project_ids)
        ops_db.commit()
        for pid in project_ids:
            delete_project_criteria(db, pid)
    db.delete(tenant)
    db.commit()


# --------------------------------------------------------------------------
# 테넌트 하위 프로젝트 (Cluster/VM Folder/VM Tag 다중 선택 기준, OR 매칭)
# --------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/projects", response_model=list[ProjectOut])
def list_tenant_projects(
    tenant_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> list[ProjectOut]:
    tenant = _get_tenant_or_404(db, tenant_id)
    return [project_to_out(db, ops_db, p) for p in sorted(tenant.projects, key=lambda p: p.name)]


@router.post("/tenants/{tenant_id}/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_tenant_project(
    tenant_id: int,
    payload: ProjectCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> ProjectOut:
    """테넌트 하위에 Project(과금 단위)를 생성한다.

    Cluster/VM Folder/VM Tag 중 여러 종류를 동시에, 각각 여러 값을 다중 선택해서 매칭 기준으로
    지정할 수 있다 (OR 매칭 - 셋 중 하나라도 일치하면 해당 VM이 이 프로젝트에 속하게 된다).
    선택 가능한 id 목록은 GET /integration-accounts/{id}/inventory 로 조회한다.
    """
    tenant = _get_tenant_or_404(db, tenant_id)
    if db.query(Project).filter_by(tenant_id=tenant.id, key=payload.key).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용 중인 프로젝트 key입니다 (테넌트 내)")

    cluster_ids = _validate_criteria_ids(ops_db, Cluster, payload.cluster_ids)
    folder_ids = _validate_criteria_ids(ops_db, VMFolder, payload.folder_ids)
    tag_ids = _validate_criteria_ids(ops_db, Tag, payload.tag_ids)
    if not cluster_ids and not folder_ids and not tag_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cluster/VM Folder/VM Tag 중 최소 1개는 선택해야 합니다")

    project = Project(
        tenant_id=tenant.id,
        key=payload.key,
        name=payload.name,
        description=payload.description,
        owner_email=payload.owner_email,
    )
    db.add(project)
    db.flush()
    set_project_criteria(db, project.id, cluster_ids=cluster_ids, folder_ids=folder_ids, tag_ids=tag_ids)
    db.add(RateCard(project_id=project.id, updated_by=admin.email))
    db.commit()

    # 새 프로젝트의 매칭 기준을 즉시 반영 (다음 5분 수집 주기를 기다리지 않고 바로 조회 가능하도록)
    recompute_project_assignments(db, ops_db)
    db.refresh(project)
    return project_to_out(db, ops_db, project)


@router.put("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectOut)
def update_tenant_project(
    tenant_id: int,
    project_id: int,
    payload: ProjectUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> ProjectOut:
    """프로젝트의 이름/설명/담당자 및 Cluster/VM Folder/VM Tag 매칭 기준을 수정한다.

    cluster_ids/folder_ids/tag_ids는 지정한 경우에만 해당 기준 전체를 교체한다 (null이면 기존 유지).
    """
    tenant = _get_tenant_or_404(db, tenant_id)
    project = _get_tenant_project_or_404(db, tenant, project_id)

    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    if payload.owner_email is not None:
        project.owner_email = payload.owner_email

    current = get_project_criteria_ids(db, project.id)
    new_cluster_ids = _validate_criteria_ids(ops_db, Cluster, payload.cluster_ids) if payload.cluster_ids is not None else current["cluster_ids"]
    new_folder_ids = _validate_criteria_ids(ops_db, VMFolder, payload.folder_ids) if payload.folder_ids is not None else current["folder_ids"]
    new_tag_ids = _validate_criteria_ids(ops_db, Tag, payload.tag_ids) if payload.tag_ids is not None else current["tag_ids"]
    if not new_cluster_ids and not new_folder_ids and not new_tag_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cluster/VM Folder/VM Tag 중 최소 1개는 선택해야 합니다")

    set_project_criteria(
        db,
        project.id,
        cluster_ids=new_cluster_ids if payload.cluster_ids is not None else None,
        folder_ids=new_folder_ids if payload.folder_ids is not None else None,
        tag_ids=new_tag_ids if payload.tag_ids is not None else None,
    )

    db.commit()
    recompute_project_assignments(db, ops_db)
    db.refresh(project)
    return project_to_out(db, ops_db, project)


@router.delete("/tenants/{tenant_id}/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_tenant_project(
    tenant_id: int,
    project_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> None:
    """프로젝트를 삭제한다. 배정되어 있던 VM은 삭제되지 않고 project_id만 None으로 되돌아간다
    (VM은 프로젝트 없이도 계속 존재할 수 있다는 기존 동작 그대로)."""
    tenant = _get_tenant_or_404(db, tenant_id)
    project = _get_tenant_project_or_404(db, tenant, project_id)
    unassign_project_vms(ops_db, project.id)
    ops_db.commit()
    delete_project_criteria(db, project.id)
    db.delete(project)
    db.commit()
    recompute_project_assignments(db, ops_db)


# --------------------------------------------------------------------------
# 테넌트 사용자 계정
# --------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/users", response_model=list[UserAdminOut])
def list_tenant_users(
    tenant_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[UserAdminOut]:
    tenant = _get_tenant_or_404(db, tenant_id)
    return [_user_to_admin_out(u) for u in sorted(tenant.users, key=lambda u: u.email)]


@router.post("/tenants/{tenant_id}/users", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
def create_tenant_user(
    tenant_id: int, payload: UserCreate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> UserAdminOut:
    """지정 테넌트에 배정된 사용자 계정을 생성한다. 이 계정으로 로그인하면 이 테넌트의
    정보만 조회할 수 있다."""
    tenant = _get_tenant_or_404(db, tenant_id)
    login_id = payload.email.strip().lower()
    if db.query(User).filter_by(email=login_id).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용 중인 이메일/아이디입니다")

    try:
        role = UserRole(payload.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'role은 "user" 또는 "admin"이어야 합니다') from exc

    user = User(
        email=login_id,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        role=role,
        tenant_id=tenant.id if role == UserRole.USER else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_to_admin_out(user)


@router.get("/users", response_model=list[UserAdminOut])
def list_all_users(_admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[UserAdminOut]:
    return [_user_to_admin_out(u) for u in db.query(User).order_by(User.email).all()]


@router.post("/users", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserAdminOut:
    """[v4.5] "사용자 관리" 화면 전용 - 테넌트 화면을 거치지 않고 어디서든 사용자를
    생성하며, role="user"면 payload.tenant_id로 소속 테넌트를 직접 선택한다
    (기존 POST /tenants/{tenant_id}/users는 테넌트 상세 화면에 그대로 남아있음)."""
    login_id = payload.email.strip().lower()
    if db.query(User).filter_by(email=login_id).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용 중인 이메일/아이디입니다")

    try:
        role = UserRole(payload.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'role은 "user" 또는 "admin"이어야 합니다') from exc

    tenant_id: int | None = None
    if role == UserRole.USER:
        if payload.tenant_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'role="user"는 소속 테넌트(tenant_id)를 선택해야 합니다')
        tenant_id = _get_tenant_or_404(db, payload.tenant_id).id

    user = User(
        email=login_id,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        role=role,
        tenant_id=tenant_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_to_admin_out(user)


@router.put("/users/{user_id}", response_model=UserAdminOut)
def update_user(
    user_id: int, payload: UserAdminUpdate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> UserAdminOut:
    """[v4.5] 표시 이름 변경 및 테넌트 재배정("사용자 관리" 화면의 "수정"). role은 이
    엔드포인트로 바꾸지 않는다 - _user_to_admin_out()의 UserAdminUpdate 스키마 설명 참고."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사용자를 찾을 수 없습니다")

    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()
    if payload.tenant_id is not None and user.role == UserRole.USER:
        user.tenant_id = _get_tenant_or_404(db, payload.tenant_id).id

    db.commit()
    db.refresh(user)
    return _user_to_admin_out(user)


@router.put(
    "/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def reset_user_password(
    user_id: int, payload: UserPasswordUpdate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사용자를 찾을 수 없습니다")
    user.password_hash = hash_password(payload.password)
    db.commit()


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> None:
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "본인 계정은 삭제할 수 없습니다")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사용자를 찾을 수 없습니다")
    db.delete(user)
    db.commit()
