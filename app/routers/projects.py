"""프로젝트 목록 조회 (역할에 따라 범위 제한)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Project, User, UserRole
from app.routers.common import project_to_out
from app.schemas import ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ProjectOut]:
    """관리자는 전체 프로젝트 목록을, 일반 사용자는 본인 테넌트 소속 프로젝트만 반환한다."""
    query = db.query(Project)
    if user.role != UserRole.ADMIN:
        query = query.filter(Project.tenant_id == user.tenant_id)
    return [project_to_out(p) for p in query.order_by(Project.name).all()]
