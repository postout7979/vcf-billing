"""인증 라우터 (아이디+비밀번호 로그인)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.database import get_db
from app.models import User
from app.schemas import LoginRequest, SelfPasswordUpdate, TokenResponse, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _to_user_out(user: User) -> UserOut:
    tenant = user.tenant
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        tenant_id=user.tenant_id,
        tenant_key=tenant.key if tenant else None,
        tenant_name=tenant.name if tenant else None,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """아이디(이메일 또는 관리자 로그인 ID)+비밀번호로 로그인합니다."""
    login_id = payload.email.strip().lower()
    user = db.query(User).filter_by(email=login_id).one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "아이디 또는 비밀번호가 올바르지 않습니다")
    token = create_access_token(user)
    return TokenResponse(access_token=token, user=_to_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _to_user_out(user)


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def change_my_password(
    payload: SelfPasswordUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """[v4.2] 로그인한 본인이 자기 비밀번호를 바꾼다 (관리자 메뉴 "비밀번호 변경").

    관리자 전용 PUT /api/admin/users/{id}/password 와 달리 현재 비밀번호 확인을 거친다.
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "현재 비밀번호가 올바르지 않습니다")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
