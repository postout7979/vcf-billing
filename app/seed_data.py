"""
샘플 데이터 시딩.

- 데모 연동 계정 1개(is_mock=True)를 독립적으로 등록하고, 실제 수집 로직
  (app/collector.collect_once_for_account)을 그대로 호출해 MockVCFOpsClient의 인벤토리
  (vCenter 1개, Datacenter 2개, Cluster/VM Folder 각 2개, VM 16대, Tag)를 적재한다 -
  "연동은 독립적으로 한 번만 하고, 그 인벤토리에서 여러 테넌트/프로젝트를 나눠 만든다"는
  새 아키텍처를 실제 운영 코드 경로 그대로 시연한다.
- 이 인벤토리를 Cluster/VM Folder/VM Tag 기준(다중 선택, OR 매칭)으로 나눠 데모 Tenant
  2개 x Project 2개씩(총 4개, 프로젝트마다 서로 다른 기준 조합을 사용)를 생성한다.
- 최근 N일간 5분 간격 PowerSample 백필 (compute_power_state 로직 재사용 -> 실시간 수집과 정합)
- 로그인용 User 계정 생성: 관리자 1명(admin/admin1!2@3#) + 테넌트별 데모 담당자 1명씩

실행:
    python -m app.seed_data --days 14
    python -m app.seed_data --days 30 --reset
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

from sqlalchemy.orm import Session

from app.auth import hash_password
from app.collector import floor_to_bucket, recompute_project_assignments, sync_account_once
from app.database import Base, SessionLocal, engine
from app.integrations.mock_client import MOCK_VM_DEFS, compute_power_state, compute_usage_pct
from app.models import (
    Cluster,
    Datacenter,
    IntegrationAccount,
    IntegrationKind,
    PowerSample,
    PowerState,
    Project,
    RateCard,
    Tag,
    Tenant,
    User,
    UserRole,
    VCenter,
    VirtualMachine,
    VMFolder,
)
from app.security.crypto import encrypt_secret

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_EMAIL = "admin"
DEFAULT_ADMIN_PASSWORD = "admin1!2@3#"
DEMO_USER_PASSWORD = "demo1234!"

MOCK_ACCOUNT_NAME = "데모 연동 계정 (vcenter01.corp.local)"

# 데모 Tenant/Project 레이아웃: 하나의(독립 등록된) 연동 계정 인벤토리를 서로 다른 기준
# 조합(Cluster/VM Folder/VM Tag, 다중 선택 OR 매칭)으로 나눠 테넌트별 Project를 구성한다.
# 4개 프로젝트가 각각 cluster-only / tag-only / folder-only / cluster+tag 조합을 사용해
# 새 아키텍처가 지원하는 매칭 기준의 유연성을 그대로 보여준다.
DEMO_LAYOUT = [
    {
        "key": "nova",
        "name": "Nova 사업부",
        "description": "Nova-DC 데모 테넌트",
        "projects": [
            {
                "key": "prod",
                "name": "Nova 운영",
                "description": "Cluster-Prod-A 클러스터 전체를 매칭 기준으로 사용",
                "owner_email": "nova-prod-lead@corp.com",
                "cluster_external_ids": ["cluster-prod-a"],
                "folder_external_ids": [],
                "tags": [],
                # [v3.7] 사용량 가중치 하이브리드 과금 데모 - always_on 프로필(높고 안정적인
                # 사용률)이 배정된 이 프로젝트 하나에만 켜서, 다른 3개 프로젝트는 기존과
                # 완전히 동일한 정액 결과를 유지하면서 새 기능을 눈으로 보여준다.
                "rate": {
                    "vcpu": 120.0,
                    "vmem": 60.0,
                    "vdisk": 8.0,
                    "currency": "KRW",
                    "usage_weight_enabled": True,
                    "usage_weight_floor_pct": 30,
                },
            },
            {
                "key": "dev",
                "name": "Nova 개발/배치",
                "description": 'VM Tag "Environment=Development" 를 매칭 기준으로 사용 (Cluster/Folder 미지정)',
                "owner_email": "nova-dev-lead@corp.com",
                "cluster_external_ids": [],
                "folder_external_ids": [],
                "tags": [("Environment", "Development")],
                "rate": {"vcpu": 80.0, "vmem": 40.0, "vdisk": 5.0, "currency": "KRW"},
            },
        ],
    },
    {
        "key": "orion",
        "name": "Orion 사업부",
        "description": "Orion-DC 데모 테넌트",
        "projects": [
            {
                "key": "qa",
                "name": "Orion QA",
                "description": "VM Folder /Orion-DC/vm/QA 를 매칭 기준으로 사용",
                "owner_email": "orion-qa-lead@corp.com",
                "cluster_external_ids": [],
                "folder_external_ids": ["folder-orion-qa"],
                "tags": [],
                "rate": {"vcpu": 90.0, "vmem": 45.0, "vdisk": 6.0, "currency": "KRW"},
            },
            {
                "key": "sandbox",
                "name": "Orion Sandbox (복합 기준)",
                "description": (
                    'Cluster-Sandbox 와 VM Tag "Environment=Sandbox" 를 동시에(OR) 매칭 기준으로 '
                    "지정하는 예시 - 여러 종류를 조합해서 선택할 수 있음을 보여준다"
                ),
                "owner_email": "orion-sandbox-lead@corp.com",
                "cluster_external_ids": ["cluster-sandbox"],
                "folder_external_ids": [],
                "tags": [("Environment", "Sandbox")],
                "rate": {"vcpu": 60.0, "vmem": 30.0, "vdisk": 4.0, "currency": "KRW"},
            },
        ],
    },
]


def _ensure_mock_integration_account(db: Session) -> IntegrationAccount:
    account = db.query(IntegrationAccount).filter_by(is_mock=True).one_or_none()
    if account is not None:
        return account
    account = IntegrationAccount(
        kind=IntegrationKind.VCF_OPS,
        name=MOCK_ACCOUNT_NAME,
        base_url="mock://vcenter01.corp.local",
        username="(demo - 실제 접속 없음)",
        password_encrypted=encrypt_secret(""),
        auth_source="local",
        verify_ssl=True,
        is_mock=True,
        updated_by="seed",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info("데모 연동 계정을 생성했습니다: %s (id=%s)", account.name, account.id)
    return account


def _collect_mock_inventory(db: Session, account: IntegrationAccount, at: dt.datetime) -> None:
    """실제 수집 경로(sync_account_once -> collect_once_for_account)를 그대로 사용해 데모
    인벤토리+VM+현재 시점 PowerSample 1건을 적재한다. 과거분은 backfill_power_samples()가
    별도로 채운다. sync_account_once를 쓰므로 데모 계정도 실제 연동 계정과 똑같이
    last_sync_status="success" 등 연동 상태가 채워진 채로 시작한다."""
    status, message, _new_samples = sync_account_once(db, account, at=at, interval_minutes=5)
    if status != "success":
        raise RuntimeError(f"데모 계정 초기 수집 실패: {message}")


def _resolve_clusters(db: Session, account: IntegrationAccount, external_ids: list[str]) -> list[Cluster]:
    if not external_ids:
        return []
    return (
        db.query(Cluster)
        .join(Datacenter, Datacenter.id == Cluster.datacenter_id)
        .join(VCenter, VCenter.id == Datacenter.vcenter_id)
        .filter(VCenter.integration_account_id == account.id, Cluster.external_id.in_(external_ids))
        .all()
    )


def _resolve_folders(db: Session, account: IntegrationAccount, external_ids: list[str]) -> list[VMFolder]:
    if not external_ids:
        return []
    return (
        db.query(VMFolder)
        .join(Datacenter, Datacenter.id == VMFolder.datacenter_id)
        .join(VCenter, VCenter.id == Datacenter.vcenter_id)
        .filter(VCenter.integration_account_id == account.id, VMFolder.external_id.in_(external_ids))
        .all()
    )


def _resolve_tags(db: Session, account: IntegrationAccount, pairs: list[tuple[str, str]]) -> list[Tag]:
    return [
        db.query(Tag).filter_by(integration_account_id=account.id, category=category, name=name).one()
        for category, name in pairs
    ]


def _ensure_tenants_and_projects(db: Session, account: IntegrationAccount) -> dict[str, Tenant]:
    tenants: dict[str, Tenant] = {}
    for tdef in DEMO_LAYOUT:
        tenant = db.query(Tenant).filter_by(key=tdef["key"]).one_or_none()
        if tenant is None:
            tenant = Tenant(key=tdef["key"], name=tdef["name"], description=tdef["description"])
            db.add(tenant)
            db.flush()
        tenants[tenant.key] = tenant

        for pdef in tdef["projects"]:
            project = db.query(Project).filter_by(tenant_id=tenant.id, key=pdef["key"]).one_or_none()
            if project is None:
                project = Project(
                    tenant_id=tenant.id,
                    key=pdef["key"],
                    name=pdef["name"],
                    description=pdef["description"],
                    owner_email=pdef["owner_email"],
                )
                db.add(project)
                db.flush()

            project.clusters = _resolve_clusters(db, account, pdef["cluster_external_ids"])
            project.folders = _resolve_folders(db, account, pdef["folder_external_ids"])
            project.tags = _resolve_tags(db, account, pdef["tags"])

            rate = db.query(RateCard).filter_by(project_id=project.id).one_or_none()
            if rate is None:
                r = pdef["rate"]
                db.add(
                    RateCard(
                        project_id=project.id,
                        vcpu_rate_per_hour=r["vcpu"],
                        vmem_rate_per_hour_gb=r["vmem"],
                        vdisk_rate_per_hour_gb=r["vdisk"],
                        currency=r["currency"],
                        usage_weight_enabled=r.get("usage_weight_enabled", False),
                        usage_weight_floor_pct=r.get("usage_weight_floor_pct", 30),
                        updated_by="seed",
                    )
                )
    db.commit()
    return tenants


def _ensure_users(db: Session, tenants: dict[str, Tenant]) -> None:
    admin = db.query(User).filter_by(email=DEFAULT_ADMIN_EMAIL).one_or_none()
    if admin is None:
        db.add(
            User(
                email=DEFAULT_ADMIN_EMAIL,
                password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
                display_name="플랫폼 관리자",
                role=UserRole.ADMIN,
                tenant_id=None,
            )
        )

    for tdef in DEMO_LAYOUT:
        tenant = tenants[tdef["key"]]
        demo_email = f"{tenant.key}-lead@corp.com"
        user = db.query(User).filter_by(email=demo_email).one_or_none()
        if user is None:
            db.add(
                User(
                    email=demo_email,
                    password_hash=hash_password(DEMO_USER_PASSWORD),
                    display_name=f"{tenant.name} 담당자",
                    role=UserRole.USER,
                    tenant_id=tenant.id,
                )
            )
        else:
            user.tenant_id = tenant.id
            user.role = UserRole.USER
    db.commit()


def backfill_power_samples(
    db: Session, account: IntegrationAccount, days: int, now: dt.datetime, interval_minutes: int = 5
) -> int:
    """최근 N일간 VM 전원상태 샘플을 결정론적 프로필 기반으로 소급 적재한다.

    가장 최근(now를 interval_minutes로 내림한 시각) 1건은 _collect_mock_inventory()가 이미
    실제 수집 경로로 적재했으므로, 그 이전 구간만 채운다.
    """
    end = floor_to_bucket(now, interval_minutes)
    fill_end = end - dt.timedelta(minutes=interval_minutes)
    start = end - dt.timedelta(days=days)

    profile_by_external_id = {v.external_id: v.profile for v in MOCK_VM_DEFS}
    vms = db.query(VirtualMachine).filter_by(integration_account_id=account.id).all()

    total_inserted = 0
    for vm in vms:
        profile = profile_by_external_id.get(vm.external_id)
        if profile is None:
            continue

        latest = (
            db.query(PowerSample.sampled_at)
            .filter_by(vm_id=vm.id)
            .filter(PowerSample.sampled_at < end)
            .order_by(PowerSample.sampled_at.desc())
            .first()
        )
        cursor = max(start, latest[0] + dt.timedelta(minutes=interval_minutes)) if latest else start

        batch: list[PowerSample] = []
        t = cursor
        while t <= fill_end:
            state = compute_power_state(profile, vm.external_id, t)
            # [v3.7] 실시간 수집 경로(_collect_mock_inventory -> MockVCFOpsClient.
            # list_vm_snapshot())와 동일하게 사용량 가중치 입력값도 함께 백필한다 -
            # 그러지 않으면 방금 수집된 최신 1건만 값이 있고 과거 대부분의 블록은
            # cpu_usage_pct/mem_usage_pct가 None으로 남아, 가중치 과금을 켜도 사실상
            # 적용되지 않는 것처럼 보인다.
            cpu_usage_pct, mem_usage_pct = compute_usage_pct(profile, vm.external_id, t, state)
            batch.append(
                PowerSample(
                    vm_id=vm.id,
                    sampled_at=t,
                    power_state=PowerState(state),
                    vcpu_count=vm.vcpu_count,
                    vmem_gb=vm.vmem_gb,
                    vdisk_gb=vm.vdisk_gb,
                    cpu_usage_pct=cpu_usage_pct,
                    mem_usage_pct=mem_usage_pct,
                )
            )
            t += dt.timedelta(minutes=interval_minutes)

        if batch:
            db.bulk_save_objects(batch)
            total_inserted += len(batch)

    db.commit()
    return total_inserted


def seed(days: int = 14, reset: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if reset:
        logger.warning("기존 데이터를 모두 삭제하고 새로 생성합니다 (--reset)")
        Base.metadata.drop_all(bind=engine)

    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        account = _ensure_mock_integration_account(db)
        _collect_mock_inventory(db, account, now)
        tenants = _ensure_tenants_and_projects(db, account)
        recompute_project_assignments(db)
        _ensure_users(db, tenants)
        n = backfill_power_samples(db, account, days=days, now=now)
        vm_count = db.query(VirtualMachine).filter_by(integration_account_id=account.id).count()
        logger.info(
            "시딩 완료: 연동 계정 1개, 테넌트 %d개, VM %d대, PowerSample %d건 신규 백필 (최근 %d일). "
            "관리자 로그인: %s / %s",
            len(tenants),
            vm_count,
            n,
            days,
            DEFAULT_ADMIN_EMAIL,
            DEFAULT_ADMIN_PASSWORD,
        )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VCF Billing Portal 샘플 데이터 시딩")
    parser.add_argument("--days", type=int, default=14, help="백필할 과거 일수 (기본 14일)")
    parser.add_argument("--reset", action="store_true", help="기존 DB를 초기화하고 새로 생성")
    args = parser.parse_args()
    seed(days=args.days, reset=args.reset)
