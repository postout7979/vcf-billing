"""관리자 화면용 API.

- 사용량/요금 조회: 전체(또는 특정 테넌트) 프로젝트 사용량/요금 조회, 프로젝트별 단가(RateCard) 설정
- 계정 연동: VCF Operations/Aria Operations 연동 계정을 독립적으로 등록/수정/삭제하고,
  해당 계정이 수집한 vCenter/Datacenter/Cluster/VM Folder/VM Tag 인벤토리를 조회
- 테넌트 관리: 테넌트 생성/조회/수정/삭제, 테넌트 하위 프로젝트(Cluster/Folder/Tag 다중 선택
  기준) 생성/수정/삭제, 테넌트에 사용자 계정 생성/배정
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.billing.aggregator import (
    available_months,
    calendar_month_period,
    compute_all_projects_usage,
    compute_project_usage,
    default_period,
)
from app.billing.statement_pdf import build_project_statement_pdf
from app.collector import recompute_project_assignments, sync_account_once
from app.database import get_db
from app.models import (
    Cluster,
    IntegrationAccount,
    IntegrationKind,
    Project,
    RateCard,
    RateCardHistory,
    Tag,
    Tenant,
    User,
    UserRole,
    VirtualMachine,
    VMFolder,
)
from app.routers.common import parse_month_param, parse_period, project_to_out, project_usage_to_schema
from app.schemas import (
    AdminOverviewOut,
    ClusterOut,
    DatacenterOut,
    IntegrationAccountCreate,
    IntegrationAccountOut,
    IntegrationAccountUpdate,
    IntegrationInventoryOut,
    IntegrationSyncOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ProjectUsageOut,
    RateCardUpdate,
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
def system_status(_admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> SystemStatusOut:
    return get_system_status(db)


# ==========================================================================
# 사용량/요금 조회 (전체 또는 tenant_id 필터로 특정 테넌트 교차 확인)
# ==========================================================================


@router.get("/months", response_model=list[str])
def list_available_months(
    tenant_id: int | None = Query(None, description="지정 시 해당 테넌트 소속 데이터만 대상으로 함"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[str]:
    """데이터가 존재하는 캘린더 월 목록 (YYYY-MM, 최신순) - 월 선택 드롭다운을 채우는 데 사용."""
    return available_months(db, tenant_id=tenant_id)


@router.get("/overview", response_model=AdminOverviewOut)
def overview(
    period: str = Query("30d", description="7d | 30d | mtd | month | custom"),
    start: str | None = None,
    end: str | None = None,
    month: str | None = Query(None, description="period=month 일 때 YYYY-MM 형식"),
    tenant_id: int | None = Query(None, description="지정 시 해당 테넌트만 조회 (교차 확인)"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminOverviewOut:
    s, e = parse_period(period, start, end, month)
    results = compute_all_projects_usage(db, s, e, tenant_id=tenant_id)
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

    return AdminOverviewOut(
        period_start=s,
        period_end=e,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        total_projects=len(project_schemas),
        total_vms=sum(p.vm_count for p in project_schemas),
        total_powered_on_vms=sum(p.powered_on_vm_count for p in project_schemas),
        total_cost=round(sum(p.total_cost for p in project_schemas), 2),
        currency_note=currency_note,
        projects=project_schemas,
        tenant_summaries=tenant_summaries,
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
) -> ProjectUsageOut:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "프로젝트를 찾을 수 없습니다")
    s, e = parse_period(period, start, end, month)
    result = compute_project_usage(db, project, s, e)
    return project_usage_to_schema(result, s, e)


@router.get("/projects/{project_id}/statement.pdf")
def project_statement_pdf(
    project_id: int,
    month: str = Query(..., description="YYYY-MM 형식 (예: 2026-08)"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    """지정 프로젝트의 특정 캘린더 월 사용량 결산서를 PDF로 내려받는다 (관리자 - 전체 테넌트 대상)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "프로젝트를 찾을 수 없습니다")
    year, month_int = parse_month_param(month)
    s, e = calendar_month_period(year, month_int)
    result = compute_project_usage(db, project, s, e)
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
    result = compute_project_usage(db, project, s, e)
    return project_usage_to_schema(result, s, e)


# ==========================================================================
# 계정 연동 (VCF Operations / Aria Operations) - 독립/전역 엔티티
# ==========================================================================


def _get_account_or_404(db: Session, account_id: int) -> IntegrationAccount:
    account = db.get(IntegrationAccount, account_id)
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
    _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[IntegrationAccountOut]:
    accounts = db.query(IntegrationAccount).order_by(IntegrationAccount.name).all()
    return [_integration_account_to_out(a) for a in accounts]


@router.post("/integration-accounts", response_model=IntegrationAccountOut, status_code=status.HTTP_201_CREATED)
def create_integration_account(
    payload: IntegrationAccountCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> IntegrationAccountOut:
    """VCF Operations/Aria Operations 연동 계정을 독립적으로 등록한다 (특정 테넌트에 종속되지 않음).

    이 계정 하나로 수집한 인벤토리(vCenter~VM)를 여러 테넌트/프로젝트가 나눠서 사용할 수 있다.
    비밀번호는 앱 레벨로 암호화해서 저장되며, 응답에는 절대 포함되지 않는다.
    """
    try:
        kind = IntegrationKind(payload.kind)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'kind는 "vcf_ops"여야 합니다') from exc

    if db.query(IntegrationAccount).filter_by(name=payload.name).one_or_none():
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
    db.add(account)
    db.commit()
    db.refresh(account)

    # 등록 즉시 연결을 시도해 인벤토리를 1회 가져온다 - 계정 정보가 맞는지 곧바로 확인할 수
    # 있게 하기 위함. 실패해도 계정 자체는 그대로 생성된 채 남고(상태만 "error"), 관리자가
    # "계정 연동" 화면에서 정보를 고쳐 저장하거나 "가져오기" 버튼으로 재시도할 수 있다.
    sync_account_once(db, account)
    db.refresh(account)
    return _integration_account_to_out(account)


@router.put("/integration-accounts/{account_id}", response_model=IntegrationAccountOut)
def update_integration_account(
    account_id: int,
    payload: IntegrationAccountUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> IntegrationAccountOut:
    account = _get_account_or_404(db, account_id)

    if payload.name is not None and payload.name != account.name:
        if db.query(IntegrationAccount).filter(IntegrationAccount.name == payload.name, IntegrationAccount.id != account.id).one_or_none():
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
    account.updated_by = admin.email
    db.commit()
    db.refresh(account)

    if connection_changed:
        sync_account_once(db, account)
        db.refresh(account)
    return _integration_account_to_out(account)


@router.delete("/integration-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_integration_account(
    account_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> None:
    """연동 계정을 삭제한다. 이 계정이 수집한 인벤토리(vCenter~VM)도 함께 삭제되며, 이 인벤토리를
    매칭 기준으로 사용하던 Project는 더 이상 해당 VM을 표시하지 않게 된다."""
    account = _get_account_or_404(db, account_id)
    db.delete(account)
    db.commit()
    recompute_project_assignments(db)


@router.post("/integration-accounts/{account_id}/sync", response_model=IntegrationSyncOut)
def sync_integration_account(
    account_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> IntegrationSyncOut:
    """"가져오기" 버튼 - 5분 주기 자동 수집을 기다리지 않고 즉시 연결을 시도해 최신
    인벤토리(vCenter~VM, vCPU/vMEM/vDisk, Tag)를 가져온다. 연결 실패 시에도 200으로
    응답하며(status="error"), 실패 사유는 계정의 last_sync_error 및 이후 계정 목록
    조회에도 그대로 남아 상태 배지에 표시된다."""
    account = _get_account_or_404(db, account_id)
    sync_status, message, new_samples = sync_account_once(db, account)
    db.refresh(account)
    return IntegrationSyncOut(
        account=_integration_account_to_out(account),
        status=sync_status,
        message=message,
        new_sample_count=new_samples,
    )


@router.get("/integration-accounts/{account_id}/inventory", response_model=IntegrationInventoryOut)
def get_integration_inventory(
    account_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> IntegrationInventoryOut:
    """연동 계정의 vCenter/Datacenter/Cluster/VM Folder/VM Tag 전체 계층을 조회한다.

    프로젝트 생성/수정 화면에서 Cluster/VM Folder/VM Tag 다중 선택 체크박스를 채우는 데 쓴다.
    아직 한 번도 수집되지 않은 계정은 빈 인벤토리를 반환한다 (수집기가 5분 주기로 자동 수집).
    """
    account = _get_account_or_404(db, account_id)
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


def _resolve_criteria_rows(db: Session, model, ids: list[int]) -> list:
    """Cluster/VMFolder/Tag id 목록을 실제 행으로 변환한다. 존재하지 않는 id가 있으면 400."""
    if not ids:
        return []
    unique_ids = list(dict.fromkeys(ids))
    rows = db.query(model).filter(model.id.in_(unique_ids)).all()
    if len(rows) != len(unique_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"존재하지 않는 {model.__name__} id가 포함되어 있습니다")
    return rows


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
def get_tenant(tenant_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> TenantDetailOut:
    tenant = _get_tenant_or_404(db, tenant_id)
    base = _tenant_to_out(tenant)
    return TenantDetailOut(
        **base.model_dump(),
        projects=[project_to_out(p) for p in sorted(tenant.projects, key=lambda p: p.name)],
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
def delete_tenant(tenant_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> None:
    """테넌트를 삭제한다. 하위 Project/User도 함께 삭제된다 (연동 계정/인벤토리는 삭제되지 않음 -
    독립 엔티티이므로 다른 테넌트가 계속 사용할 수 있다)."""
    tenant = _get_tenant_or_404(db, tenant_id)
    project_ids = [p.id for p in tenant.projects]
    if project_ids:
        db.query(VirtualMachine).filter(VirtualMachine.project_id.in_(project_ids)).update(
            {"project_id": None}, synchronize_session=False
        )
    db.delete(tenant)
    db.commit()


# --------------------------------------------------------------------------
# 테넌트 하위 프로젝트 (Cluster/VM Folder/VM Tag 다중 선택 기준, OR 매칭)
# --------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/projects", response_model=list[ProjectOut])
def list_tenant_projects(
    tenant_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[ProjectOut]:
    tenant = _get_tenant_or_404(db, tenant_id)
    return [project_to_out(p) for p in sorted(tenant.projects, key=lambda p: p.name)]


@router.post("/tenants/{tenant_id}/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_tenant_project(
    tenant_id: int, payload: ProjectCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> ProjectOut:
    """테넌트 하위에 Project(과금 단위)를 생성한다.

    Cluster/VM Folder/VM Tag 중 여러 종류를 동시에, 각각 여러 값을 다중 선택해서 매칭 기준으로
    지정할 수 있다 (OR 매칭 - 셋 중 하나라도 일치하면 해당 VM이 이 프로젝트에 속하게 된다).
    선택 가능한 id 목록은 GET /integration-accounts/{id}/inventory 로 조회한다.
    """
    tenant = _get_tenant_or_404(db, tenant_id)
    if db.query(Project).filter_by(tenant_id=tenant.id, key=payload.key).one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용 중인 프로젝트 key입니다 (테넌트 내)")

    clusters = _resolve_criteria_rows(db, Cluster, payload.cluster_ids)
    folders = _resolve_criteria_rows(db, VMFolder, payload.folder_ids)
    tags = _resolve_criteria_rows(db, Tag, payload.tag_ids)
    if not clusters and not folders and not tags:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cluster/VM Folder/VM Tag 중 최소 1개는 선택해야 합니다")

    project = Project(
        tenant_id=tenant.id,
        key=payload.key,
        name=payload.name,
        description=payload.description,
        owner_email=payload.owner_email,
    )
    project.clusters = clusters
    project.folders = folders
    project.tags = tags
    db.add(project)
    db.flush()
    db.add(RateCard(project_id=project.id, updated_by=admin.email))
    db.commit()

    # 새 프로젝트의 매칭 기준을 즉시 반영 (다음 5분 수집 주기를 기다리지 않고 바로 조회 가능하도록)
    recompute_project_assignments(db)
    db.refresh(project)
    return project_to_out(project)


@router.put("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectOut)
def update_tenant_project(
    tenant_id: int,
    project_id: int,
    payload: ProjectUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
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
    if payload.cluster_ids is not None:
        project.clusters = _resolve_criteria_rows(db, Cluster, payload.cluster_ids)
    if payload.folder_ids is not None:
        project.folders = _resolve_criteria_rows(db, VMFolder, payload.folder_ids)
    if payload.tag_ids is not None:
        project.tags = _resolve_criteria_rows(db, Tag, payload.tag_ids)

    if not project.clusters and not project.folders and not project.tags:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cluster/VM Folder/VM Tag 중 최소 1개는 선택해야 합니다")

    db.commit()
    recompute_project_assignments(db)
    db.refresh(project)
    return project_to_out(project)


@router.delete("/tenants/{tenant_id}/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_tenant_project(
    tenant_id: int, project_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> None:
    tenant = _get_tenant_or_404(db, tenant_id)
    project = _get_tenant_project_or_404(db, tenant, project_id)
    db.query(VirtualMachine).filter_by(project_id=project.id).update({"project_id": None})
    db.delete(project)
    db.commit()
    recompute_project_assignments(db)


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
