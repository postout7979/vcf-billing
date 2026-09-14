"""
[v4.9] Operations DB(app/models_ops.py)를 "Project 기준으로" 조회하는 헬퍼 모음.

Project(Billing DB)와 VirtualMachine(Operations DB)은 서로 다른 물리 데이터베이스일 수
있어 더 이상 ORM relationship이나 SQL JOIN으로 묶을 수 없다(project_id는 소프트 참조 -
app/models_ops.py 상단 주석 참고). 이 모듈이 "Operations DB 쪽에서 project_id로 조회"하는
로직을 한 곳에 모아, 호출부(라우터/집계 로직)가 매번 직접 쿼리를 짜지 않도록 한다.

이 앱의 규모(design.md 기준 "수십~수백 VM")에서는 프로젝트 1개당 쿼리 1번이면 충분하며,
굳이 exotic한 배치 최적화를 할 필요가 없다는 것이 기존 설계 결정이다 - 이 모듈도 그
전제를 따른다(다만 여러 프로젝트를 한 번에 다뤄야 하는 호출부를 위해 IN 절 기반의 벌크
버전도 함께 제공한다).
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models_ops import Cluster, Tag, VirtualMachine, VMFolder


def get_vms_for_project(ops_db: Session, project_id: int) -> list[VirtualMachine]:
    """지정 프로젝트에 배정된(project_id 일치) VM 전체를 반환한다."""
    return ops_db.query(VirtualMachine).filter(VirtualMachine.project_id == project_id).all()


def get_vms_for_projects(ops_db: Session, project_ids: list[int]) -> dict[int, list[VirtualMachine]]:
    """여러 프로젝트에 배정된 VM을 한 번의 쿼리로 조회해 project_id별로 묶어 반환한다."""
    result: dict[int, list[VirtualMachine]] = {pid: [] for pid in project_ids}
    if not project_ids:
        return result
    vms = ops_db.query(VirtualMachine).filter(VirtualMachine.project_id.in_(project_ids)).all()
    for vm in vms:
        result.setdefault(vm.project_id, []).append(vm)
    return result


def get_vm_ids_for_projects(ops_db: Session, project_ids: list[int]) -> dict[int, list[int]]:
    """project_id -> VM id 목록. 프로젝트별 VM 개수/샘플 조회 등에 사용하는 가벼운 버전."""
    result: dict[int, list[int]] = {pid: [] for pid in project_ids}
    if not project_ids:
        return result
    rows = ops_db.execute(
        select(VirtualMachine.project_id, VirtualMachine.id).where(VirtualMachine.project_id.in_(project_ids))
    )
    for pid, vm_id in rows:
        result.setdefault(pid, []).append(vm_id)
    return result


def count_vms_by_project(ops_db: Session, project_ids: list[int]) -> dict[int, int]:
    """project_id -> 배정된 VM 대수."""
    result: dict[int, int] = {pid: 0 for pid in project_ids}
    if not project_ids:
        return result
    rows = ops_db.execute(
        select(VirtualMachine.project_id, func.count(VirtualMachine.id))
        .where(VirtualMachine.project_id.in_(project_ids))
        .group_by(VirtualMachine.project_id)
    )
    for pid, count in rows:
        result[pid] = count
    return result


def unassign_project_vms(ops_db: Session, project_id: int) -> int:
    """프로젝트 삭제 시 호출 - 해당 프로젝트에 배정되어 있던 VM의 project_id를 모두 None으로
    되돌린다 (VM 자체는 삭제하지 않는다 - "VM은 프로젝트 없이도 존재할 수 있다"는 기존 동작을
    그대로 유지). 반환값은 영향받은 VM 대수."""
    result = (
        ops_db.query(VirtualMachine)
        .filter(VirtualMachine.project_id == project_id)
        .update({"project_id": None}, synchronize_session=False)
    )
    return result


def unassign_projects_vms(ops_db: Session, project_ids: list[int]) -> int:
    """unassign_project_vms()의 다건 버전 (예: 테넌트 삭제 시 하위 여러 프로젝트를 한 번에)."""
    if not project_ids:
        return 0
    return (
        ops_db.query(VirtualMachine)
        .filter(VirtualMachine.project_id.in_(project_ids))
        .update({"project_id": None}, synchronize_session=False)
    )


def resolve_clusters(ops_db: Session, cluster_ids: list[int]) -> list[Cluster]:
    if not cluster_ids:
        return []
    return ops_db.query(Cluster).filter(Cluster.id.in_(cluster_ids)).all()


def resolve_folders(ops_db: Session, folder_ids: list[int]) -> list[VMFolder]:
    if not folder_ids:
        return []
    return ops_db.query(VMFolder).filter(VMFolder.id.in_(folder_ids)).all()


def resolve_tags(ops_db: Session, tag_ids: list[int]) -> list[Tag]:
    if not tag_ids:
        return []
    return ops_db.query(Tag).filter(Tag.id.in_(tag_ids)).all()
