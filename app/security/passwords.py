"""[v4.0] 비밀번호 해시/검증 (bcrypt 전용, FastAPI/JWT 의존성 없음).

app/auth.py에서 이 모듈을 그대로 재노출(re-export)하므로 기존 호출부
(app/routers/*.py, app/seed_data.py 등)는 변경 없이 `from app.auth import
hash_password` 형태로 계속 쓸 수 있다.

이걸 app/auth.py에서 분리한 이유는 app/bootstrap_db.py(=collector 컨테이너의
부트스트랩 경로에서도 import됨) 때문이다. app/auth.py는 모듈 최상단에서
`from fastapi import ...`, `from jose import ...`를 임포트하는데, collector
컨테이너 이미지(requirements-collector.txt)에는 이 두 패키지를 아예 설치하지
않는다(이미지를 가볍게 유지하고 "필요한 모듈만" 두는 원칙). bootstrap_db.py가
app.auth를 직접 import하면 collector 컨테이너에서 ImportError가 나므로, bcrypt만
쓰는 순수 해시 로직을 이 파일로 분리해 bootstrap_db.py는 이 파일만 import한다.
"""
from __future__ import annotations

import bcrypt


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        # 저장된 해시 형식이 깨진 경우 등 - 인증 실패로 취급
        return False
