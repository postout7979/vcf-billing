"""
[v4.9] Project의 Cluster/VM Folder/VM Tag 다중 선택 매칭 기준 조회/저장 헬퍼.

Billing DB(Project)와 Operations DB(Cluster/VMFolder/Tag)가 서로 다른 물리
데이터베이스일 수 있게 되면서, SQLAlchemy relationship(Project.clusters 등)으로 두 DB를
잇는 것이 더 이상 불가능해졌다. project_cluster_link/project_folder_link/
project_tag_link 세 연결 테이블은 project_id가 실제로 가리키는 쪽(Billing DB)에 그대로
두되(app/models.py), cluster_id/folder_id/tag_id는 Operations DB 쪽 행을 가리키는 "id
값"으로만 다룬다. 이 모듈이 그 연결 테이블을 직접 다루는 유일한 곳이다.
"""
from __future__ import annotations

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models import project_cluster_link, project_folder_link, project_tag_link


def get_project_criteria_ids(billing_db: Session, project_id: int) -> dict[str, list[int]]:
    """지정 프로젝트가 선택한 Cluster/Folder/Tag id 목록을 반환한다.

    반환값: {"cluster_ids": [...], "folder_ids": [...], "tag_ids": [...]} (각각 오름차순 정렬)
    """
    cluster_ids = sorted(
        billing_db.scalars(
            select(project_cluster_link.c.cluster_id).where(project_cluster_link.c.project_id == project_id)
        ).all()
    )
    folder_ids = sorted(
        billing_db.scalars(
            select(project_folder_link.c.folder_id).where(project_folder_link.c.project_id == project_id)
        ).all()
    )
    tag_ids = sorted(
        billing_db.scalars(
            select(project_tag_link.c.tag_id).where(project_tag_link.c.project_id == project_id)
        ).all()
    )
    return {"cluster_ids": cluster_ids, "folder_ids": folder_ids, "tag_ids": tag_ids}


def get_all_projects_criteria_ids(billing_db: Session) -> dict[int, dict[str, list[int]]]:
    """전체 프로젝트의 매칭 기준 id 목록을 project_id별로 한 번에 조회한다 (재계산용 벌크 조회).

    반환값: {project_id: {"cluster_ids": [...], "folder_ids": [...], "tag_ids": [...]}}
    프로젝트가 아무 기준도 선택하지 않았다면 그 project_id는 키 자체가 없을 수 있다 -
    호출부는 .get(project_id, {"cluster_ids": [], "folder_ids": [], "tag_ids": []}) 로 접근한다.
    """
    result: dict[int, dict[str, list[int]]] = {}

    def _ensure(pid: int) -> dict[str, list[int]]:
        return result.setdefault(pid, {"cluster_ids": [], "folder_ids": [], "tag_ids": []})

    for pid, cid in billing_db.execute(select(project_cluster_link.c.project_id, project_cluster_link.c.cluster_id)):
        _ensure(pid)["cluster_ids"].append(cid)
    for pid, fid in billing_db.execute(select(project_folder_link.c.project_id, project_folder_link.c.folder_id)):
        _ensure(pid)["folder_ids"].append(fid)
    for pid, tid in billing_db.execute(select(project_tag_link.c.project_id, project_tag_link.c.tag_id)):
        _ensure(pid)["tag_ids"].append(tid)
    return result


def set_project_criteria(
    billing_db: Session,
    project_id: int,
    cluster_ids: list[int] | None = None,
    folder_ids: list[int] | None = None,
    tag_ids: list[int] | None = None,
) -> None:
    """지정 프로젝트의 매칭 기준을 교체한다 (해당 종류만 delete + insert).

    각 인자는 None이면 "그 종류는 건드리지 않음"(기존 값 유지), 빈 리스트([])면 "전부
    해제"를 뜻한다 - 기존 ProjectUpdate 스키마의 "지정한 경우에만 해당 기준 전체를
    교체" 시맨틱을 그대로 따른다. 커밋은 호출부 책임이다(다른 변경사항과 한 트랜잭션으로
    묶기 위함).
    """
    if cluster_ids is not None:
        billing_db.execute(delete(project_cluster_link).where(project_cluster_link.c.project_id == project_id))
        unique = sorted(set(cluster_ids))
        if unique:
            billing_db.execute(
                insert(project_cluster_link), [{"project_id": project_id, "cluster_id": cid} for cid in unique]
            )
    if folder_ids is not None:
        billing_db.execute(delete(project_folder_link).where(project_folder_link.c.project_id == project_id))
        unique = sorted(set(folder_ids))
        if unique:
            billing_db.execute(
                insert(project_folder_link), [{"project_id": project_id, "folder_id": fid} for fid in unique]
            )
    if tag_ids is not None:
        billing_db.execute(delete(project_tag_link).where(project_tag_link.c.project_id == project_id))
        unique = sorted(set(tag_ids))
        if unique:
            billing_db.execute(
                insert(project_tag_link), [{"project_id": project_id, "tag_id": tid} for tid in unique]
            )


def delete_project_criteria(billing_db: Session, project_id: int) -> None:
    """프로젝트 삭제 시 세 연결 테이블에서 해당 프로젝트의 행을 모두 지운다.

    project_cluster_link 등은 project_id에 ondelete="CASCADE" FK가 걸려 있어 Project를
    ORM으로 delete()하면 DB 레벨에서 자동으로 함께 지워지지만(PostgreSQL/최신 SQLite),
    호출부에서 명시적으로 정리하고 싶을 때(예: raw delete를 쓰는 경로) 사용할 수 있도록
    별도 헬퍼로 둔다.
    """
    billing_db.execute(delete(project_cluster_link).where(project_cluster_link.c.project_id == project_id))
    billing_db.execute(delete(project_folder_link).where(project_folder_link.c.project_id == project_id))
    billing_db.execute(delete(project_tag_link).where(project_tag_link.c.project_id == project_id))


def criteria_summary_from_ids(cluster_ids: list[int], folder_ids: list[int], tag_ids: list[int]) -> str:
    """관리자 화면/결산서에 표시할 매칭 기준 요약 (예: "Cluster 2개 · Tag 1개").

    [v4.9] 이전에는 Project.criteria_summary 프로퍼티가 self.clusters 등 ORM
    relationship을 세서 만들었지만, 더 이상 그 relationship이 존재하지 않으므로(다른
    물리 DB) id 개수만으로 계산하는 순수 함수로 옮겼다 - 로직 자체는 동일하다.
    """
    parts = []
    if cluster_ids:
        parts.append(f"Cluster {len(cluster_ids)}개")
    if folder_ids:
        parts.append(f"Folder {len(folder_ids)}개")
    if tag_ids:
        parts.append(f"Tag {len(tag_ids)}개")
    return " · ".join(parts) if parts else "미지정"
