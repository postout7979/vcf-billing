"""SQLAlchemy 엔진 / 세션 설정."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# sqlite 파일 디렉터리 보장 (docker-compose 없이 로컬에서 SQLite 폴백을 쓰는 경우에만 해당)
if settings.database_url.startswith("sqlite:///"):
    db_path = settings.database_url.replace("sqlite:///", "", 1)
    Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)

_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}

# [v4.0] PostgreSQL 사용 시 pool_pre_ping=True로, 오래 유휴 상태였던 커넥션(예: DB
# 컨테이너 재시작, 네트워크 순단)을 자동으로 감지해 재연결한다. API/Collector가 서로
# 다른 컨테이너로 분리된 뒤에는 이런 순단이 SQLite 단일 파일 방식보다 더 흔히 발생할
# 수 있어 필요한 방어 설정이다. SQLite에는 영향 없음(풀링을 안 쓰므로 사실상 no-op).
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=not _is_sqlite)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def wait_for_db(max_attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """DB가 접속 가능해질 때까지 대기한다.

    [v4.0] Docker Compose에서 db 컨테이너에 healthcheck를 걸어두긴 했지만("postgres
    -U ... pg_isready"), api/collector 컨테이너의 `depends_on: condition:
    service_healthy`만으로는 완전히 안전하지 않을 수 있다(healthcheck 통과 직후의
    미세한 타이밍, 재시작 시나리오 등). 컨테이너가 뜨자마자 곧바로 DB 쿼리를 던지는
    대신, 접속에 실패하면 지수 없이 일정 간격으로 재시도해 "DB가 아직 안 떴는데 API가
    먼저 크래시" 하는 상황을 없앤다. SQLite 폴백 사용 시에는 즉시 통과한다.
    """
    if _is_sqlite:
        return

    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("DB 접속 확인 완료 (시도 %d/%d)", attempt, max_attempts)
            return
        except OperationalError as exc:
            logger.warning(
                "DB 접속 대기 중... (시도 %d/%d) %s", attempt, max_attempts, exc.__class__.__name__
            )
            if attempt == max_attempts:
                raise
            time.sleep(delay_seconds)
