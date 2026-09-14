"""SQLAlchemy 엔진 / 세션 설정.

[v4.9] Billing DB(과금 단위 구성 - Tenant/Project/RateCard/User/AppState)와 Operations
DB(수집 데이터 - IntegrationAccount/VCenter~VirtualMachine/PowerSample, app/models_ops.py)를
서로 다른 SQLAlchemy 엔진/세션/DeclarativeBase로 분리한다. 기본값(OPERATIONS_DATABASE_URL
미설정)에서는 두 엔진이 같은 물리 DB/파일을 가리키지만, 코드 경로는 항상 별도 엔진을
거치므로 이후 관리자가 Operations DB를 실제로 분리해도(같은 PostgreSQL 서버의 별도 DB,
또는 완전히 다른 서버) 코드 변경 없이 그대로 동작한다. 두 DB 사이에는 SQL JOIN이나
SQLAlchemy relationship()이 존재하지 않는다 - 서로 다른 물리 데이터베이스일 수 있으므로
애초에 성립할 수 없다 (app/project_criteria.py, app/ops_queries.py 참고).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import effective_operations_database_url, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


def _ensure_sqlite_dir(database_url: str) -> None:
    """sqlite 파일 디렉터리 보장 (docker-compose 없이 로컬에서 SQLite 폴백을 쓰는 경우에만 해당)."""
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "", 1)
        Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)


def _make_engine(database_url: str):
    is_sqlite = database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    _ensure_sqlite_dir(database_url)
    # [v4.0] PostgreSQL 사용 시 pool_pre_ping=True로, 오래 유휴 상태였던 커넥션(예: DB
    # 컨테이너 재시작, 네트워크 순단)을 자동으로 감지해 재연결한다. API/Collector가 서로
    # 다른 컨테이너로 분리된 뒤에는 이런 순단이 SQLite 단일 파일 방식보다 더 흔히 발생할
    # 수 있어 필요한 방어 설정이다. SQLite에는 영향 없음(풀링을 안 쓰므로 사실상 no-op).
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=not is_sqlite)


# ---------------------------------------------------------------------------
# Billing DB (Tenant/Project/RateCard/RateCardHistory/User/AppState)
# ---------------------------------------------------------------------------

_is_sqlite = settings.database_url.startswith("sqlite")
engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _wait_for_engine(target_engine, is_sqlite: bool, label: str, max_attempts: int, delay_seconds: float) -> None:
    if is_sqlite:
        return
    for attempt in range(1, max_attempts + 1):
        try:
            with target_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("%s 접속 확인 완료 (시도 %d/%d)", label, attempt, max_attempts)
            return
        except OperationalError as exc:
            logger.warning(
                "%s 접속 대기 중... (시도 %d/%d) %s", label, attempt, max_attempts, exc.__class__.__name__
            )
            if attempt == max_attempts:
                raise
            time.sleep(delay_seconds)


def wait_for_db(max_attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """Billing DB가 접속 가능해질 때까지 대기한다.

    [v4.0] Docker Compose에서 db 컨테이너에 healthcheck를 걸어두긴 했지만("postgres
    -U ... pg_isready"), api/collector 컨테이너의 `depends_on: condition:
    service_healthy`만으로는 완전히 안전하지 않을 수 있다(healthcheck 통과 직후의
    미세한 타이밍, 재시작 시나리오 등). 컨테이너가 뜨자마자 곧바로 DB 쿼리를 던지는
    대신, 접속에 실패하면 지수 없이 일정 간격으로 재시도해 "DB가 아직 안 떴는데 API가
    먼저 크래시" 하는 상황을 없앤다. SQLite 폴백 사용 시에는 즉시 통과한다.
    """
    _wait_for_engine(engine, _is_sqlite, "Billing DB", max_attempts, delay_seconds)


# ---------------------------------------------------------------------------
# [v4.9] Operations DB (IntegrationAccount/VCenter~VirtualMachine/PowerSample)
#
# OPERATIONS_DATABASE_URL이 비어 있으면 database_url(Billing DB)과 같은 물리 DB를
# 가리키지만, 별도 엔진/세션 객체로만 접근한다 - "항상 아키텍처적으로 분리되어 있고,
# 기본값에서만 우연히 같은 물리 DB로 귀결된다"는 설계를 코드 레벨에서 강제하기 위함.
# ---------------------------------------------------------------------------

_operations_database_url = effective_operations_database_url(settings)
_is_ops_sqlite = _operations_database_url.startswith("sqlite")
ops_engine = _make_engine(_operations_database_url)
OpsSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=ops_engine)


class OpsBase(DeclarativeBase):
    pass


def get_ops_db():
    db = OpsSessionLocal()
    try:
        yield db
    finally:
        db.close()


def wait_for_db_ops(max_attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """Operations DB가 접속 가능해질 때까지 대기한다 (wait_for_db()의 Operations DB용 짝).

    Billing DB와 물리적으로 같은 DB를 가리키는 기본 설정에서도 그냥 한 번 더 접속을
    확인할 뿐이라 비용이 거의 없고(멱등), 실제로 분리된 별도 서버를 가리키는 경우에는
    이 대기가 반드시 필요하다.
    """
    _wait_for_engine(ops_engine, _is_ops_sqlite, "Operations DB", max_attempts, delay_seconds)
