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

[v4.9] Billing DB(Base/engine)와 Operations DB(OpsBase/ops_engine)가 분리되면서, 이
모듈은 이제 두 데이터베이스를 각각 따로 대기(wait_for_db/wait_for_db_ops)하고 각각의
metadata로 테이블을 생성한다. 기본 설정(OPERATIONS_DATABASE_URL 미설정)에서는 두
엔진이 같은 물리 DB를 가리키므로 결과적으로 한 DB에 두 세트의 테이블이 모두 생성되고,
분리 설정 시에는 각자의 물리 DB에 필요한 테이블만 생성된다.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.database import Base, OpsBase, SessionLocal, engine, ops_engine, wait_for_db, wait_for_db_ops
from app.models import User, UserRole
import app.models_ops  # noqa: F401  (OpsBase.metadata에 Operations DB 테이블을 등록시키기 위한 import)
from app.security.passwords import hash_password

logger = logging.getLogger(__name__)


def _ensure_column(target_engine: Engine, table: str, column: str, sql_type: str, default_sql: str) -> None:
    """[v4.10] 이미 운영 중인 배포(기존 테이블이 있는 DB)에 새 컬럼을 경량 추가한다.

    `Base.metadata.create_all()`/`OpsBase.metadata.create_all()`은 "없는 테이블"만
    만들 뿐 "이미 있는 테이블의 없는 컬럼"은 채워주지 않는다 - 이 프로젝트는 Alembic
    같은 정식 마이그레이션 도구 없이 zip/git으로 코드만 갱신하는 배포 방식(design.md
    참고)이라, 모델에 컬럼을 추가할 때마다 기존 DB에도 반영되는 이런 경량 보정이
    없으면 컬럼이 없는 채로 남아 다음 쿼리에서 바로 오류가 난다. 멱등(이미 있으면
    아무 것도 안 함)이라 여러 번 실행해도 안전하다.
    """
    with target_engine.begin() as conn:
        if target_engine.dialect.name == "sqlite":
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type} DEFAULT {default_sql}"))
        else:
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type} DEFAULT {default_sql}")
            )


def _run_light_migrations() -> None:
    """[v4.10] 백업/복구 기능 도입 시 추가한 IntegrationAccount.needs_reconnect 컬럼 보정."""
    is_sqlite = ops_engine.dialect.name == "sqlite"
    _ensure_column(
        ops_engine,
        "integration_accounts",
        "needs_reconnect",
        "BOOLEAN NOT NULL" if not is_sqlite else "BOOLEAN",
        "0" if is_sqlite else "FALSE",
    )

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
    """Billing DB / Operations DB 두 데이터베이스를 각각 대기 후 테이블을 생성한다.

    [v4.9] 두 DB가 완전히 분리된 배포(서로 다른 서버)에서도 안전하도록, 두 대기/생성을
    독립적으로 수행한다 - 어느 한쪽이 기본값(같은 물리 DB)이든 실제로 분리되어 있든
    동일하게 동작한다.
    """
    wait_for_db()
    Base.metadata.create_all(bind=engine)

    wait_for_db_ops()
    OpsBase.metadata.create_all(bind=ops_engine)
    _run_light_migrations()

    ensure_default_admin()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("DB 부트스트랩 시작 (Billing/Operations DB 테이블 생성 + 기본 admin 계정 확인)")
    bootstrap()
    logger.info("DB 부트스트랩 완료")


if __name__ == "__main__":
    main()
