"""
VM 인벤토리 / 전원상태 수집기.

COLLECTOR_INTERVAL_MINUTES(기본 5분) 주기로, 등록된 모든 IntegrationAccount(계정 연동
메뉴에서 독립적으로 등록)를 순회하며 각 계정의 VCFOpsClient(is_mock이면 샘플 데이터,
아니면 실 연동).list_vm_snapshot() 을 호출해 vCenter/Datacenter/Cluster/VM Folder/Tag
인벤토리 계층과 VirtualMachine을 최신화하고, 해당 시각의 PowerSample을 적재한다.
PowerSample 1행 = 과금 단위 블록 1개(billing 엔진의 원천 데이터).

모든 연동 계정의 수집이 끝난 뒤에는 recompute_project_assignments() 를 한 번 호출해,
전체 VM의 project_id를 각 Project의 Cluster/VM Folder/VM Tag 다중 선택 기준(OR 매칭)에
따라 전역적으로 재계산한다. Project는 특정 연동 계정에 종속되지 않으므로, 이 재계산은
계정 단위가 아니라 항상 전체 VM을 대상으로 한 번에 수행한다.

한 연동 계정의 수집 실패(연동 계정 오류 등)가 다른 계정의 수집을 막지 않도록 계정 단위로
예외를 격리한다.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.integrations import VCFOpsClient, build_client_for_integration_account
from app.integrations.base import VMSnapshot
from app.models import (
    Cluster,
    Datacenter,
    IntegrationAccount,
    PowerSample,
    PowerState,
    Project,
    Tag,
    VCenter,
    VirtualMachine,
    VMFolder,
    utcnow,
)

logger = logging.getLogger(__name__)


def floor_to_bucket(t: dt.datetime, minutes: int) -> dt.datetime:
    t = t.astimezone(dt.timezone.utc)
    discard = dt.timedelta(minutes=t.minute % minutes, seconds=t.second, microseconds=t.microsecond)
    return t - discard


class _HierarchyCache:
    """한 번의 수집 사이클 동안, 이미 조회/생성한 계층 행을 재사용하기 위한 캐시.

    같은 연동 계정 안에서 여러 VM이 같은 vCenter/Datacenter/Cluster/Folder/Tag를
    공유하는 경우가 대부분이라, VM마다 매번 DB를 조회하지 않도록 한다.
    """

    def __init__(self) -> None:
        self.vcenters: dict[tuple[int, str], VCenter] = {}
        self.datacenters: dict[tuple[int, str], Datacenter] = {}
        self.clusters: dict[tuple[int, str], Cluster] = {}
        self.folders: dict[tuple[int, str], VMFolder] = {}
        self.tags: dict[tuple[int, str, str], Tag] = {}


def _get_or_create_vcenter(db: Session, cache: _HierarchyCache, account_id: int, external_id: str, name: str) -> VCenter:
    key = (account_id, external_id)
    vcenter = cache.vcenters.get(key)
    if vcenter is None:
        vcenter = db.query(VCenter).filter_by(integration_account_id=account_id, external_id=external_id).one_or_none()
        if vcenter is None:
            vcenter = VCenter(integration_account_id=account_id, external_id=external_id, name=name)
            db.add(vcenter)
            db.flush()
        cache.vcenters[key] = vcenter
    if vcenter.name != name:
        vcenter.name = name
    return vcenter


def _get_or_create_datacenter(db: Session, cache: _HierarchyCache, vcenter_id: int, external_id: str, name: str) -> Datacenter:
    key = (vcenter_id, external_id)
    datacenter = cache.datacenters.get(key)
    if datacenter is None:
        datacenter = db.query(Datacenter).filter_by(vcenter_id=vcenter_id, external_id=external_id).one_or_none()
        if datacenter is None:
            datacenter = Datacenter(vcenter_id=vcenter_id, external_id=external_id, name=name)
            db.add(datacenter)
            db.flush()
        cache.datacenters[key] = datacenter
    if datacenter.name != name:
        datacenter.name = name
    return datacenter


def _get_or_create_cluster(db: Session, cache: _HierarchyCache, datacenter_id: int, external_id: str, name: str) -> Cluster:
    key = (datacenter_id, external_id)
    cluster = cache.clusters.get(key)
    if cluster is None:
        cluster = db.query(Cluster).filter_by(datacenter_id=datacenter_id, external_id=external_id).one_or_none()
        if cluster is None:
            cluster = Cluster(datacenter_id=datacenter_id, external_id=external_id, name=name)
            db.add(cluster)
            db.flush()
        cache.clusters[key] = cluster
    if cluster.name != name:
        cluster.name = name
    return cluster


def _get_or_create_folder(
    db: Session, cache: _HierarchyCache, datacenter_id: int, external_id: str, path: str, name: str
) -> VMFolder:
    key = (datacenter_id, external_id)
    folder = cache.folders.get(key)
    if folder is None:
        folder = db.query(VMFolder).filter_by(datacenter_id=datacenter_id, external_id=external_id).one_or_none()
        if folder is None:
            folder = VMFolder(datacenter_id=datacenter_id, external_id=external_id, path=path, name=name)
            db.add(folder)
            db.flush()
        cache.folders[key] = folder
    if folder.path != path or folder.name != name:
        folder.path = path
        folder.name = name
    return folder


def _get_or_create_tag(db: Session, cache: _HierarchyCache, account_id: int, category: str, name: str) -> Tag:
    key = (account_id, category, name)
    tag = cache.tags.get(key)
    if tag is None:
        tag = db.query(Tag).filter_by(integration_account_id=account_id, category=category, name=name).one_or_none()
        if tag is None:
            tag = Tag(integration_account_id=account_id, category=category, name=name)
            db.add(tag)
            db.flush()
        cache.tags[key] = tag
    return tag


def _upsert_hierarchy(
    db: Session, cache: _HierarchyCache, account_id: int, snap: VMSnapshot
) -> tuple[VCenter, Datacenter, Cluster | None, VMFolder | None, list[Tag]]:
    """스냅샷의 vCenter/Datacenter/Cluster/Folder/Tag 를 업서트하고 각 행을 반환한다."""
    vcenter = _get_or_create_vcenter(db, cache, account_id, snap.vcenter_external_id, snap.vcenter_name)
    datacenter = _get_or_create_datacenter(db, cache, vcenter.id, snap.datacenter_external_id, snap.datacenter_name)

    cluster: Cluster | None = None
    if snap.cluster_external_id:
        cluster = _get_or_create_cluster(db, cache, datacenter.id, snap.cluster_external_id, snap.cluster_name or snap.cluster_external_id)

    folder: VMFolder | None = None
    if snap.folder_external_id:
        folder = _get_or_create_folder(
            db, cache, datacenter.id, snap.folder_external_id, snap.folder_path or "", snap.folder_name or snap.folder_external_id
        )

    tags = [_get_or_create_tag(db, cache, account_id, t.category, t.name) for t in snap.tags]
    return vcenter, datacenter, cluster, folder, tags


def collect_once_for_account(
    db: Session,
    account: IntegrationAccount,
    client: VCFOpsClient,
    at: dt.datetime | None = None,
    interval_minutes: int = 5,
) -> int:
    """한 연동 계정에 대해 한 번의 수집 사이클을 실행하고, 새로 적재된 PowerSample 개수를 반환한다.

    VM의 project_id는 여기서 계산하지 않는다 (Project는 계정에 종속되지 않으므로, 모든
    계정의 수집이 끝난 뒤 recompute_project_assignments() 가 전역적으로 재계산한다).
    """
    now = floor_to_bucket(at or dt.datetime.now(dt.timezone.utc), interval_minutes)
    snapshots = client.list_vm_snapshot()
    cache = _HierarchyCache()

    inserted = 0
    for snap in snapshots:
        vcenter, datacenter, cluster, folder, tags = _upsert_hierarchy(db, cache, account.id, snap)

        vm = (
            db.query(VirtualMachine)
            .filter_by(integration_account_id=account.id, external_id=snap.external_id)
            .one_or_none()
        )
        if vm is None:
            vm = VirtualMachine(
                integration_account_id=account.id,
                external_id=snap.external_id,
                name=snap.name,
                vcenter_id=vcenter.id,
                datacenter_id=datacenter.id,
                cluster_id=cluster.id if cluster else None,
                folder_id=folder.id if folder else None,
                vcpu_count=snap.vcpu_count,
                vmem_gb=snap.vmem_gb,
                vdisk_gb=snap.vdisk_gb,
                os_name=snap.os_name,
                current_power_state=PowerState(snap.power_state),
            )
            db.add(vm)
            db.flush()
        else:
            vm.name = snap.name
            vm.vcenter_id = vcenter.id
            vm.datacenter_id = datacenter.id
            vm.cluster_id = cluster.id if cluster else None
            vm.folder_id = folder.id if folder else None
            vm.vcpu_count = snap.vcpu_count
            vm.vmem_gb = snap.vmem_gb
            vm.vdisk_gb = snap.vdisk_gb
            vm.os_name = snap.os_name
            vm.current_power_state = PowerState(snap.power_state)

        vm.tags = tags

        already = db.query(PowerSample.id).filter_by(vm_id=vm.id, sampled_at=now).first()
        if already:
            continue

        db.add(
            PowerSample(
                vm_id=vm.id,
                sampled_at=now,
                power_state=PowerState(snap.power_state),
                vcpu_count=snap.vcpu_count,
                vmem_gb=snap.vmem_gb,
                vdisk_gb=snap.vdisk_gb,
                # [v3.7] 사용량 가중치 하이브리드 과금 입력값. None이면 그대로 저장되고,
                # 과금 엔진이 "가중치 없음"으로 처리한다(app/billing/engine.py 참고).
                cpu_usage_pct=snap.cpu_usage_pct,
                mem_usage_pct=snap.mem_usage_pct,
            )
        )
        inserted += 1

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("중복 샘플 삽입 충돌 - 롤백 (동시 실행 가능성, account_id=%s)", account.id)

    return inserted


def recompute_project_assignments(db: Session) -> None:
    """전체 VM의 project_id를 모든 Project의 Cluster/Folder/Tag 다중 선택 기준으로 재계산한다.

    매칭 우선순위: Cluster 일치 > Folder 일치 > Tag 일치 (VM 1대에 대해 먼저 발견되는
    기준을 채택). 같은 Cluster/Folder/Tag를 여러 Project가 동시에 선택한 경우에는, id가
    가장 작은(=먼저 생성된) Project가 우선한다. 어떤 기준에도 해당하지 않는 VM은
    project_id가 None(미배정)이 된다.
    """
    cluster_to_project: dict[int, int] = {}
    folder_to_project: dict[int, int] = {}
    tag_to_project: dict[int, int] = {}

    for project in db.query(Project).order_by(Project.id.asc()).all():
        for cluster in project.clusters:
            cluster_to_project.setdefault(cluster.id, project.id)
        for folder in project.folders:
            folder_to_project.setdefault(folder.id, project.id)
        for tag in project.tags:
            tag_to_project.setdefault(tag.id, project.id)

    for vm in db.query(VirtualMachine).all():
        project_id: int | None = None
        if vm.cluster_id is not None:
            project_id = cluster_to_project.get(vm.cluster_id)
        if project_id is None and vm.folder_id is not None:
            project_id = folder_to_project.get(vm.folder_id)
        if project_id is None:
            for tag in vm.tags:
                if tag.id in tag_to_project:
                    project_id = tag_to_project[tag.id]
                    break
        vm.project_id = project_id

    db.commit()


def sync_account_once(
    db: Session,
    account: IntegrationAccount,
    at: dt.datetime | None = None,
    interval_minutes: int = 5,
    recompute: bool = True,
) -> tuple[str, str, int]:
    """한 연동 계정에 대해 연결 시도 + 1회 수집을 실행하고, 결과를 계정의 last_sync_* 필드에
    기록한다 (관리자 화면의 연동 상태 배지와 "가져오기" 버튼이 이 함수를 공유해서 쓴다).

    client.list_vm_snapshot() 호출 자체가 실 연동 시의 연결 시도이므로, 별도의 "연결 테스트"
    API 없이 이 함수 하나로 연결 확인과 인벤토리 수집을 겸한다. 연결 실패 시 예외는 여기서
    잡아 last_sync_status="error"/last_sync_error에 원인을 남기고, 예외를 호출자에게
    다시 던지지 않는다 (상태 배지로 확인하도록).

    반환값: (status, message, new_sample_count) - status는 "success" | "error".
    """
    client = build_client_for_integration_account(account)
    try:
        try:
            new_samples = collect_once_for_account(db, account, client, at=at, interval_minutes=interval_minutes)
            if recompute:
                recompute_project_assignments(db)

            account.last_sync_status = "success"
            account.last_sync_at = utcnow()
            account.last_sync_error = None
            account.last_sync_vm_count = len(account.vms)
            db.commit()
            return "success", f"인벤토리 수집 완료 (VM {account.last_sync_vm_count}대)", new_samples
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("연동 계정 %s(id=%s) 수집 중 오류 발생", account.name, account.id)
            # 위 rollback으로 account 인스턴스가 detach될 수 있어 다시 조회해서 상태만 갱신한다.
            account = db.get(IntegrationAccount, account.id)
            if account is not None:
                account.last_sync_status = "error"
                account.last_sync_at = utcnow()
                account.last_sync_error = str(exc)[:2048]
                db.commit()
            return "error", f"연동 실패: {exc}", 0
    finally:
        # 매 수집 사이클마다 새로 만드는 클라이언트이므로, 커넥션이 누적되지 않도록
        # 반드시 닫는다 (실 연동 구현체는 내부 httpx.Client를 정리, Mock은 no-op).
        client.close()


def collect_once(db: Session, interval_minutes: int = 5, at: dt.datetime | None = None) -> int:
    """등록된 모든 연동 계정에 대해 한 번씩 수집 사이클을 실행한 뒤, Project 배정을 재계산한다.

    계정별 예외는 격리한다 (한 계정의 연동 오류가 다른 계정의 수집을 막지 않음). 각 계정의
    성공/실패 결과는 sync_account_once()를 통해 last_sync_* 필드에 남아, 관리자 화면의
    연동 상태 배지가 자동 수집 결과도 그대로 반영한다.
    """
    total = 0
    for account in db.query(IntegrationAccount).all():
        _status, _message, new_samples = sync_account_once(db, account, at=at, interval_minutes=interval_minutes, recompute=False)
        total += new_samples

    try:
        recompute_project_assignments(db)
    except Exception:  # noqa: BLE001
        logger.exception("Project 배정 재계산 중 오류 발생")

    return total


# [v4.0] Docker Compose의 collector 컨테이너 HEALTHCHECK(docker/collector/Dockerfile)가
# 참조하는 하트비트 파일. 매 수집 사이클이 끝날 때마다(성공/실패 무관) touch해서, 이
# 파일의 mtime이 오래됐으면 루프 자체가 멎었다고 판단할 수 있게 한다.
_HEARTBEAT_PATH = Path(os.environ.get("COLLECTOR_HEARTBEAT_FILE", "/tmp/collector_heartbeat"))


def _touch_heartbeat() -> None:
    try:
        _HEARTBEAT_PATH.touch()
    except OSError:  # noqa: BLE001 - 하트비트 기록 실패는 수집 자체를 막을 이유가 아님
        logger.warning("하트비트 파일(%s) 기록 실패", _HEARTBEAT_PATH)


async def run_forever(interval_minutes: int | None = None) -> None:
    """FastAPI 백그라운드 태스크로 실행되는 무한 수집 루프."""
    settings = get_settings()
    interval = interval_minutes or settings.collector_interval_minutes

    logger.info("VM 수집기 시작 (interval=%s분, 연동 계정별 실 연동/샘플 데이터 자동 사용)", interval)
    while True:
        db = SessionLocal()
        try:
            n = collect_once(db, interval_minutes=interval)
            if n:
                logger.info("수집 완료: 신규 샘플 %d건 적재", n)
        except Exception:  # noqa: BLE001
            logger.exception("수집 사이클 중 오류 발생")
        finally:
            db.close()
            _touch_heartbeat()
        await asyncio.sleep(interval * 60)
