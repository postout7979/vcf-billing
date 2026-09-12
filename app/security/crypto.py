"""
Tenant별 VCF Operations/Aria Operations 연동 계정 비밀번호를 DB에 저장하기 전/후
암호화·복호화하는 앱 레벨 암호화 유틸리티.

설계 메모
---------
- 대칭키(Fernet, AES128-CBC + HMAC)를 사용하며, 키는 .env 의 SECRET_KEY 를
  SHA-256으로 해시한 값을 그대로 Fernet 키(32바이트, base64 urlsafe)로 사용한다.
  즉 SECRET_KEY 하나만 안전하게 관리하면 되고, 별도의 키 파일/시크릿을 추가로
  운영할 필요가 없다.
- SECRET_KEY 가 유출되면 JWT 위조뿐 아니라 저장된 연동 계정 비밀번호도 복호화될
  수 있으므로, 실 운영 배포 시 SECRET_KEY는 반드시 충분히 긴 랜덤 값으로 설정하고
  안전하게 보관해야 한다 (README/배포 가이드 참고).
- 이 모듈이 암호화하는 것은 "이 앱이 VCF Operations/Aria Operations 에 접속하기
  위한 자격증명"뿐이며, 사용자 로그인 비밀번호는 (복호화가 필요 없으므로) 별도로
  app/auth.py 에서 bcrypt 단방향 해시로 저장한다.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    key_bytes = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def encrypt_secret(plain: str) -> str:
    """평문 문자열을 암호화해 DB에 저장 가능한 문자열로 반환한다."""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """encrypt_secret() 으로 암호화된 문자열을 복호화한다.

    SECRET_KEY가 저장 당시와 달라진 경우(예: .env 변경) InvalidToken이 발생하며,
    이 경우 연동 계정 비밀번호를 관리자 화면에서 재입력해야 한다.
    """
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "저장된 연동 계정 비밀번호를 복호화할 수 없습니다 (SECRET_KEY 변경 여부 확인 필요). "
            "관리자 화면에서 연동 계정 비밀번호를 다시 입력해 주세요."
        ) from exc
