"""
인증.

아이디(이메일 또는 관리자 로그인 ID)+비밀번호로 로그인하는 JWT 발급/검증 모듈입니다.
비밀번호는 bcrypt로 단방향 해시하여 저장하며 평문은 어디에도 보관하지 않습니다.
실 서비스로 전환할 때는 사내 SSO/OIDC(예: Keycloak, Azure AD, Okta 등) 연동으로
이 모듈 전체를 대체할 수 있습니다. 그 외 라우터(app/routers/*.py)는 get_current_user /
require_admin 의존성에만 의존하므로, 인증 방식이 바뀌어도 영향받지 않습니다.
"""
from __future__ import annotations

import datetime as dt

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User, UserRole
# [v4.0] 실제 해시 로직은 app/security/passwords.py로 분리되어 있다(collector 컨테이너가
# fastapi/jose 없이도 이 로직을 쓸 수 있게 하기 위함). 기존 호출부
# (app/routers/*.py, app/seed_data.py)와의 호환을 위해 여기서 그대로 재노출한다.
from app.security.passwords import hash_password, verify_password  # noqa: F401

settings = get_settings()
bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(user: User) -> str:
    expire = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": user.email,
        "role": user.role.value,
        "tenant_id": user.tenant_id,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증 토큰이 유효하지 않습니다") from exc


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다")
    payload = _decode(creds.credentials)
    user = db.query(User).filter_by(email=payload.get("sub")).one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없습니다")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "관리자 권한이 필요합니다")
    return user
