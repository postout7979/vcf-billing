"""[v4.0] DB 부트스트랩: 테이블 생성 + 기본 admin 계정 보장.

Docker Compose 구성에서는 `migrate` 서비스가 이 모듈을 1회 실행하고 정상 종료(exit 0)
해야 api/collector 컨테이너가 시작된다 (docker-compose.yml의 `depends_on: migrate:
condition: service_completed_successfully`). API를 여러 워커로, 게다가 collector까지
별도 프로세스/컨테이너로 쪼개면서 "여러 프로세스가 동시에 스키마를 만들고 기본 admin
계정을 집어넣으려는" 경쟁 상태(예: users.email UNIQUE 제약 위반)가 생길 수 있는데,
스키마 준비를 이 한 곳에서 먼저 끝내고 나면 그 이후 api/collector가 각자 호출하는
동일 함수는 "이미 있으면 아무 것도 안 함" 경로만 타므로 안전해진다.

app/main.py의 lifespan과 app/collector_main.py도 방어적으로 이 모듈의 함수를 다시
호출한다 (docker-compose 없이 이 저장소를 그냥 `uvicorn app.main:app`으로 띄워보는
경우처럼 migrate 단계가 없는 실행 경로를 위한 폴백 - 이 경우 API가 단일 프로세스이므로
경쟁 상태 자체가 발생하지 않는다).
"""
from __future__ import annotations

import logging

from app.database import Base, SessionLocal, engine, wait_for_db
from app.models import User, UserRole
from app.security.passwords import hash_password

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_EMAIL = "admin"
DEFAULT_ADMIN_PASSWORD = "admin1!2@3#"


def ensure_default_admin() -> None:
    """관리자 계정이 하나도 없으면 기본 관리자 계정(admin/admin1!2@3#)을 생성한다.

    seed_data.py를 실행하지 않고 곧바로 서버를 띄운 배포본도 최초 로그인이
    가능하도록 하기 위한 안전장치. 이미 admin 계정이 있으면 아무 것도 하지 않는다
    (비밀번호를 덮어쓰지 않음 - 운영 중 변경한 비밀번호가 재기동 때마다 초기화되는 것을 방지).
    """
    db = SessionLocal()
    try:
        exists = db.query(User).filter_by(email=DEFAULT_ADMIN_EMAIL).one_or_none()
        if exists is not None:
            return
        db.add(
            User(
                email=DEFAULT_ADMIN_EMAIL,
                password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
                display_name="플랫폼 관리자",
                role=UserRole.ADMIN,
                tenant_id=None,
            )
        )
        db.commit()
        logger.info("기본 관리자 계정을 생성했습니다 (id=%s). 최초 로그인 후 반드시 비밀번호를 변경하세요.", DEFAULT_ADMIN_EMAIL)
    finally:
        db.close()


def bootstrap() -> None:
    wait_for_db()
    Base.metadata.create_all(bind=engine)
    ensure_default_admin()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("DB 부트스트랩 시작 (테이블 생성 + 기본 admin 계정 확인)")
    bootstrap()
    logger.info("DB 부트스트랩 완료")


if __name__ == "__main__":
    main()
