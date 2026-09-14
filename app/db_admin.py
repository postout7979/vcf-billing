"""[v4.6] 관리자 "데이터베이스" 화면용 로직.

무엇을 하는가:
- 현재 이 애플리케이션이 실제로 연결해 쓰고 있는 DB(PostgreSQL 또는 로컬 SQLite 폴백)의
  정보(접속 정보/서버 버전/크기/테이블별 행수)를 조회한다.
- 관리자가 입력한 외부 PostgreSQL 접속 정보로 연결이 되는지 테스트한다.
- 그 외부 PostgreSQL로 현재 DB의 전체 데이터를 복사(마이그레이션)한다.
- 현재 DB 전체를 백업 파일로 내보낸다(PostgreSQL은 pg_dump SQL 덤프, SQLite는 일관된
  스냅샷 파일 복사).

중요한 한계 (설계 결정, claude/vcf-billing-portal-design.md "v4.6 개편" 참고):
- 이 앱은 Docker Compose로 db/migrate/api/collector/frontend 5개 컨테이너로 나뉘어
  있고, 실제 사용할 DB는 각 컨테이너의 DATABASE_URL 환경변수로 컨테이너 기동 시점에
  정해진다. api 컨테이너는 도커 소켓이 없어(v4.4에서 의도적으로 마운트하지 않기로
  결정) 자기 자신을 포함한 어떤 컨테이너도 재시작할 수 없다. 따라서 "마이그레이션"
  기능은 데이터를 외부 DB로 복사하는 것까지만 하고, 실제로 그 DB를 쓰도록 전환하는
  것은 관리자가 .env의 DATABASE_URL을 바꾸고 `docker compose up -d --force-recreate
  migrate api collector`를 직접 실행해야 한다 - 이 안내를 응답의 next_steps에 담아
  돌려준다. 로컬 데이터는 이 기능으로 전혀 건드리지 않으므로(읽기만 함), 실패해도
  기존 배포에는 영향이 없다.

[v4.9] Billing DB(Base/engine)와 Operations DB(OpsBase/ops_engine)가 분리되면서, 이
파일의 핵심 로직(현재 DB 정보 조회/외부 PostgreSQL 마이그레이션/내보내기)을 DbTarget
데이터클래스로 일반화해 두 데이터베이스 어느 쪽에도 적용할 수 있게 했다. 호출부(라우터)가
어느 세션(billing db 세션 / ops db 세션)을 넘기느냐로 대상이 정해진다. "로컬 DB 계속
사용"(apply_local_db_setup, v4.8) 한 가지만 기존처럼 Billing DB 전용으로 남겨뒀다 - 이미
정착된 계약을 건드리지 않기 위함이며, Operations DB의 "로컬 추가 데이터베이스" 마법사
선택지는 성격이 달라(비밀번호 변경이 아니라 "같은 서버에 새 DB를 만드는 것") 별도 함수
(create_local_additional_operations_database)로 새로 추가했다.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql as pg_sql
from sqlalchemy import MetaData, create_engine, func, insert, select, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session

from app.bootstrap_db import DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_PASSWORD, ensure_default_admin
from app.config import effective_operations_database_url, get_settings
from app.database import Base, OpsBase, SessionLocal, engine as default_engine, ops_engine as default_ops_engine
import app.models  # noqa: F401  (Base.metadata에 Billing DB 테이블을 등록시키기 위한 import)
import app.models_ops  # noqa: F401  (OpsBase.metadata에 Operations DB 테이블을 등록시키기 위한 import)
from app.models import AppState
from app.schemas import (
    DatabaseOverviewOut,
    DatabaseTableStatOut,
    ExternalDbConnectionRequest,
    ExternalDbMigrateRequest,
    ExternalDbMigrateResult,
    ExternalDbTableResult,
    ExternalDbTestResult,
    LocalDbSetupRequest,
    LocalDbSetupResult,
    OperationsDbLocalSetupRequest,
    OperationsDbLocalSetupResult,
)


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


# ==========================================================================
# [v4.9] 두 데이터베이스(billing/operations) 어느 쪽에도 적용할 수 있게 일반화한 대상 기술자
# ==========================================================================


@dataclass(frozen=True)
class DbTarget:
    """get_database_overview/migrate_to_external_postgres/export_database_bytes가
    다루는 대상 데이터베이스 (billing 또는 operations)를 기술한다.

    session은 반드시 이 target.engine에 바인딩된 세션이어야 한다 - 호출부(라우터)가
    Depends(get_db) 또는 Depends(get_ops_db)로 그 세션을 결정해서 넘긴다.
    """

    label: str  # "billing" | "operations" - 응답 메시지/안내 문구 생성에 사용
    display_name: str  # "Billing DB" | "Operations DB" - 관리자에게 보여줄 이름
    env_var: str  # "DATABASE_URL" | "OPERATIONS_DATABASE_URL"
    metadata: MetaData
    engine: Engine
    database_url: str


def billing_target() -> DbTarget:
    settings = get_settings()
    return DbTarget(
        label="billing",
        display_name="Billing DB",
        env_var="DATABASE_URL",
        metadata=Base.metadata,
        engine=default_engine,
        database_url=settings.database_url,
    )


def operations_target() -> DbTarget:
    settings = get_settings()
    return DbTarget(
        label="operations",
        display_name="Operations DB",
        env_var="OPERATIONS_DATABASE_URL",
        metadata=OpsBase.metadata,
        engine=default_ops_engine,
        database_url=effective_operations_database_url(settings),
    )


def get_database_overview(session: Session, target: DbTarget | None = None) -> DatabaseOverviewOut:
    """현재 target(기본값 billing)이 실제로 연결해 쓰고 있는 DB의 정보를 조회한다.

    session은 target.engine에 바인딩되어 있어야 한다 (billing이면 Depends(get_db),
    operations면 Depends(get_ops_db)).
    """
    target = target or billing_target()
    is_sqlite = _is_sqlite_url(target.database_url)

    tables: list[DatabaseTableStatOut] = []
    for table in target.metadata.sorted_tables:
        row_count = session.scalar(select(func.count()).select_from(table)) or 0
        size_bytes = None
        if not is_sqlite:
            size_bytes = session.scalar(text("SELECT pg_total_relation_size(:t)"), {"t": table.name})
        tables.append(DatabaseTableStatOut(name=table.name, row_count=row_count, size_bytes=size_bytes))

    if is_sqlite:
        db_path = target.database_url.replace("sqlite:///", "", 1)
        try:
            size_bytes = Path(db_path).resolve().stat().st_size
        except OSError:
            size_bytes = None
        server_version = session.scalar(text("SELECT sqlite_version()"))
        return DatabaseOverviewOut(
            engine="sqlite",
            file_path=str(Path(db_path).resolve()),
            server_version=f"SQLite {server_version}" if server_version else None,
            size_bytes=size_bytes,
            tables=tables,
        )

    url = make_url(target.database_url)
    size_bytes = session.scalar(text("SELECT pg_database_size(current_database())"))
    server_version = session.scalar(text("SHOW server_version"))
    return DatabaseOverviewOut(
        engine="postgresql",
        host=url.host,
        port=url.port,
        database=url.database,
        username=url.username,
        server_version=f"PostgreSQL {server_version}" if server_version else None,
        size_bytes=size_bytes,
        tables=tables,
    )


def _build_target_url(req: ExternalDbConnectionRequest) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=req.username,
        password=req.password,
        host=req.host,
        port=req.port,
        database=req.database,
        query={"sslmode": req.sslmode} if req.sslmode else {},
    )


def test_external_connection(req: ExternalDbConnectionRequest) -> ExternalDbTestResult:
    url = _build_target_url(req)
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar()
        return ExternalDbTestResult(ok=True, message="연결에 성공했습니다.", server_version=version)
    except Exception as exc:  # noqa: BLE001 - 어떤 접속 실패든 그대로 사유를 보여줘야 함
        return ExternalDbTestResult(ok=False, message=str(exc))
    finally:
        engine.dispose()


class ExternalDbNotEmptyError(ValueError):
    """대상 DB에 이미 데이터가 있어 마이그레이션을 거부할 때."""


def migrate_to_external_postgres(
    source_db: Session, req: ExternalDbMigrateRequest, target: DbTarget | None = None
) -> ExternalDbMigrateResult:
    """현재 연결된 target(기본값 billing)의 모든 테이블을 외부 PostgreSQL로 그대로 복사한다.

    scripts/migrate_sqlite_to_postgres.py(v4.0, SQLite -> PostgreSQL 1회성 스크립트)와
    같은 원리를 "현재 연결(SQLite든 PostgreSQL이든 무관)-> 임의의 외부 PostgreSQL"로
    일반화한 것. 대상 DB가 비어있지 않으면(PK 충돌 위험) 안전하게 거부한다.

    [v4.9] target을 operations로 지정하면 Operations DB(OpsBase.metadata)를 대상으로
    똑같이 동작한다 - source_db는 반드시 target.engine에 바인딩된 세션이어야 한다.
    """
    target = target or billing_target()
    target_url = _build_target_url(req)
    target_engine = create_engine(target_url, connect_args={"connect_timeout": 10})
    try:
        target.metadata.create_all(bind=target_engine)

        with target_engine.connect() as check_conn:
            for table in target.metadata.sorted_tables:
                existing = check_conn.scalar(select(func.count()).select_from(table)) or 0
                if existing > 0:
                    raise ExternalDbNotEmptyError(
                        f"대상 DB의 '{table.name}' 테이블에 이미 {existing}행이 있습니다. "
                        "마이그레이션 대상 PostgreSQL DB는 반드시 비어 있어야 합니다."
                    )

        table_results: list[ExternalDbTableResult] = []
        source_bind = source_db.get_bind()
        with source_bind.connect() as src, target_engine.begin() as dst:
            for table in target.metadata.sorted_tables:
                rows = src.execute(select(table)).mappings().all()
                if rows:
                    dst.execute(insert(table), [dict(r) for r in rows])
                    # table.primary_key.columns의 각 컬럼이 갖는 .autoincrement 값은
                    # 실제 bool이 아니라 대개 문자열 "auto"(SQLAlchemy의 지연 판정
                    # 방식)라, `if col.autoincrement:`처럼 그대로 진위값으로 쓰면
                    # project_tag_link 같은 복합 PK(둘 다 FK)인 M2M 연결 테이블의
                    # 컬럼까지 전부 "auto increment 있음"으로 오판한다 - 그런 테이블은
                    # 애초에 시퀀스 자체가 생성되지 않으므로 setval이 "relation ...
                    # does not exist"로 실패한다. `table._autoincrement_column`은
                    # SQLAlchemy가 실제로 시퀀스/SERIAL을 붙이는 단일 컬럼(단순 정수
                    # 단독 PK인 경우만)만 정확히 알려주므로 이것으로 판정한다.
                    auto_col = table._autoincrement_column
                    if auto_col is not None:
                        seq_name = f"{table.name}_{auto_col.name}_seq"
                        dst.execute(
                            text(
                                f"SELECT setval('{seq_name}', "
                                f"COALESCE((SELECT MAX({auto_col.name}) FROM {table.name}), 1), true)"
                            )
                        )
                table_results.append(ExternalDbTableResult(name=table.name, rows=len(rows)))

        full_url = target_url.render_as_string(hide_password=False)
        masked_url = target_url.render_as_string(hide_password=True)
        total_rows = sum(t.rows for t in table_results)
        return ExternalDbMigrateResult(
            ok=True,
            message=f"{target.display_name}: {len(table_results)}개 테이블, 총 {total_rows}행을 외부 PostgreSQL로 복사했습니다.",
            tables=table_results,
            database_url=full_url,
            database_url_masked=masked_url,
            next_steps=[
                f"위 접속 문자열을 복사해 .env 파일의 {target.env_var} 값을 이것으로 교체하세요.",
                "docker compose up -d --force-recreate migrate api collector 를 실행해 전환을 반영하세요"
                " (db 컨테이너는 그대로 두어도 됩니다 - 더 이상 이 앱이 쓰지 않을 뿐입니다).",
                "전환 후 이 화면(데이터베이스 메뉴)을 다시 열어 새 DB로 정상 연결되는지 확인하세요.",
                "확인이 끝나면 기존 로컬 DB는 이번 작업으로 전혀 변경되지 않았으므로,"
                " 필요 시 그대로 백업 삼아 보관하거나 정리하면 됩니다.",
            ],
        )
    finally:
        target_engine.dispose()


def export_database_bytes(target: DbTarget | None = None) -> tuple[bytes, str, str]:
    """target(기본값 billing) 전체를 백업 파일 바이트로 만들어 (내용, 파일명, media_type)을 반환한다.

    PostgreSQL은 pg_dump로 복원 가능한 평문 SQL 덤프를, SQLite는 sqlite3 백업 API로
    뜬 일관된 스냅샷 파일(.db, 그대로 data/billing.db 자리에 넣으면 복원됨)을 만든다.
    """
    target = target or billing_target()
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if _is_sqlite_url(target.database_url):
        db_path = Path(target.database_url.replace("sqlite:///", "", 1)).resolve()
        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_path = Path(tmp_dir) / "backup.db"
            src_conn = sqlite3.connect(str(db_path))
            dst_conn = sqlite3.connect(str(backup_path))
            try:
                with dst_conn:
                    src_conn.backup(dst_conn)
            finally:
                src_conn.close()
                dst_conn.close()
            data = backup_path.read_bytes()
        return data, f"vcf-{target.label}-sqlite-backup-{timestamp}.db", "application/x-sqlite3"

    pg_dump_path = shutil.which("pg_dump")
    if pg_dump_path is None:
        raise RuntimeError(
            "pg_dump 실행 파일을 찾을 수 없습니다 (api 컨테이너 이미지에 postgresql-client 미설치) - "
            "api 이미지를 최신 Dockerfile로 재빌드한 뒤 다시 시도하세요."
        )
    url = make_url(target.database_url)
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "backup.sql"
        cmd = [
            pg_dump_path,
            "-h", url.host or "localhost",
            "-p", str(url.port or 5432),
            "-U", url.username or "postgres",
            "-d", url.database or "",
            "-F", "p",
            "--no-owner",
            "--no-privileges",
            "-f", str(out_path),
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump 실패: {proc.stderr.strip()[-2000:]}")
        data = out_path.read_bytes()
    return data, f"vcf-{target.label}-postgres-backup-{timestamp}.sql", "application/sql"


# ==========================================================================
# [v4.8] 최초 로그인 DB 설정 게이트
#
# 배경(설계 결정, claude/vcf-billing-portal-design.md "v4.8 개편" 참고): "로그인 전에
# DB를 고른다"는 문자 그대로는 불가능하다 - app/bootstrap_db.py의 bootstrap()이
# migrate 컨테이너에서 로그인 화면이 뜨기 전에 이미 DATABASE_URL로 접속해 스키마 생성
# +기본 admin 계정 보장까지 끝내기 때문이다(로그인 자체가 이미 연결된 DB를 전제로
# 함). 그래서 "admin이 최초 로그인한 직후" 뜨는 1회성 안내 팝업으로 근사한다: 이미
# 연결된 로컬 DB를 계속 쓸지, 아니면 외부 PostgreSQL로 옮길지(외부 DB 연동은 기존
# v4.6 "외부 PostgreSQL 연결" 화면으로 안내) 고르게 하고, "로컬 DB 계속 사용"을
# 고르면 그 자리에서 DB 계정 비밀번호를 정의(+선택적으로 기존 데이터 전체 초기화)할
# 수 있게 한다.
#
# [v4.9] 이 게이트는 여전히 "Billing DB를 최초에 어떻게 할지" + "admin 로그인 비밀번호
# 변경"만 다룬다 - Operations DB 선택지(로컬 추가 데이터베이스/외부 데이터베이스)는
# 아래 별도 섹션의 새 엔드포인트가 담당한다. initial_db_setup_seen 플래그 하나가 마법사
# 전체(Billing DB 설정 + 로그인 비밀번호 변경 + Operations DB 설정)를 "이미 봤음"으로
# 표시하는 단일 신호라는 점은 바뀌지 않는다.
# ==========================================================================


def get_setup_status(db: Session) -> bool:
    state = db.get(AppState, 1)
    return bool(state.initial_db_setup_seen) if state is not None else False


def mark_setup_seen(db: Session) -> None:
    state = db.get(AppState, 1)
    if state is None:
        db.add(AppState(id=1, initial_db_setup_seen=True))
    else:
        state.initial_db_setup_seen = True
    db.commit()


class LocalDbSetupError(ValueError):
    """로컬 DB 설정 처리 중 사용자에게 그대로 보여줄 수 있는(요청이 잘못된) 오류."""


def _alter_role_password(target_engine: Engine, username: str, new_password: str) -> None:
    """PostgreSQL 로그인 역할(ROLE)의 비밀번호를 바꾼다.

    psycopg3의 sql.Identifier/sql.Literal로 식별자(계정명)와 리터럴(새 비밀번호)을
    합성해 SQL 인젝션 위험 없이 안전하게 조립한다. ALTER ROLE 같은 DDL 유틸리티
    구문은 SQLAlchemy의 text() 바인드 파라미터로 안정적으로 표현되지 않으므로,
    engine.raw_connection()으로 실제 psycopg3 DBAPI 커넥션/커서를 직접 사용한다.
    """
    raw_conn = target_engine.raw_connection()
    try:
        cur = raw_conn.cursor()
        try:
            cur.execute(
                pg_sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                    pg_sql.Identifier(username), pg_sql.Literal(new_password)
                )
            )
        finally:
            cur.close()
        raw_conn.commit()
    finally:
        raw_conn.close()


def apply_local_db_setup(db: Session, req: LocalDbSetupRequest) -> LocalDbSetupResult:
    """"로컬 DB 계속 사용" 선택 후 제출한 폼을 처리한다 (Billing DB 전용, v4.8과 동일한 계약).

    - wipe_data=False (기본, 안전 경로): DB 계정 비밀번호만 변경. 기존 VM/요금
      데이터는 전혀 건드리지 않는다. SQLite는 애초에 "DB 계정"이라는 개념이 없어
      (파일 하나가 DB 전체) 이 경로 자체가 의미가 없으므로 명시적으로 거부한다.
    - wipe_data=True (파괴적 경로): 위 비밀번호 변경(PostgreSQL일 때만)에 더해 모든
      테이블을 drop 후 재생성하고 기본 admin 계정을 다시 만든다. SQLite에서도 이
      경로는 유효하다(파일을 통째로 비우는 것과 같은 효과) - 다만 바꿀 "계정
      비밀번호"가 없으므로 new_password 값은 무시된다.

    [v4.9] Operations DB는 이 함수가 건드리지 않는다 - Billing DB만 초기화한다(둘이
    같은 물리 DB를 가리키는 기본 설정에서는 테이블 단위로 drop/create되므로 Operations
    DB 테이블은 그대로 남는다. 완전히 분리된 설정에서는 애초에 다른 물리 DB라 영향이
    없다).
    """
    settings = get_settings()
    is_sqlite = _is_sqlite_url(settings.database_url)

    if req.wipe_data and not req.confirm_wipe:
        raise LocalDbSetupError(
            "전체 초기화(기존 VM/요금 데이터 삭제)를 실행하려면 confirm_wipe=true로 별도 확인해야 합니다"
        )
    if is_sqlite and not req.wipe_data:
        raise LocalDbSetupError(
            "SQLite 로컬 파일 DB에는 별도 계정 비밀번호 개념이 없습니다 - 비밀번호만 변경하는 "
            "옵션은 PostgreSQL을 사용할 때만 의미가 있습니다. 데이터를 지우고 다시 시작하려면 "
            "'전체 초기화' 옵션을 선택하세요."
        )

    password_changed = False
    if not is_sqlite:
        url = make_url(settings.database_url)
        if not url.username:
            raise LocalDbSetupError("현재 DB 접속 정보에서 계정명을 확인할 수 없습니다")
        _alter_role_password(default_engine, url.username, req.new_password)
        password_changed = True

    wiped = False
    if req.wipe_data:
        # DROP/CREATE 같은 스키마 전체를 건드리는 DDL 전에, 이 요청이 들고 온 세션이
        # 잡고 있던 트랜잭션/커넥션을 먼저 반납한다 - 그대로 두면 지금 로그인해 있는
        # admin 자신의 행이 지워지는 도중에 같은 세션이 이전 상태를 참조하게 되어
        # PostgreSQL에서는 잠금 충돌이, SQLite에서는 파일 잠금 문제가 생길 수 있다.
        db.close()
        Base.metadata.drop_all(bind=default_engine)
        Base.metadata.create_all(bind=default_engine)
        ensure_default_admin()
        wiped = True

    # setup-status 플래그는 항상 새 세션으로 기록한다 - wipe 경로에서는 위 db가 이미
    # close()되어 더 이상 재사용할 수 없기 때문에 매번 새로 연다.
    fresh_db = SessionLocal()
    try:
        mark_setup_seen(fresh_db)
    finally:
        fresh_db.close()

    reconnect_hint = (
        ".env 파일의 DATABASE_URL에 새 비밀번호를 반영한 뒤 docker compose up -d "
        "--force-recreate migrate api collector 로 재기동하세요 - 그렇지 않으면 다음 재기동 시"
        "API/Collector가 이전 비밀번호로 접속을 시도해 실패합니다."
    )

    next_steps: list[str] = []
    if wiped:
        message = "로컬 DB를 초기화했습니다. 기존 VM/요금 데이터가 모두 삭제되고 스키마가 새로 생성되었습니다."
        if password_changed:
            message += " DB 계정 비밀번호도 변경했습니다."
        next_steps.append(
            f"기본 관리자 계정({DEFAULT_ADMIN_EMAIL} / {DEFAULT_ADMIN_PASSWORD})으로 다시 로그인하세요 - "
            "기존 계정이 모두 삭제되었으므로 지금 로그인 세션은 곧바로 무효화됩니다."
        )
        if password_changed:
            next_steps.append(reconnect_hint)
    else:
        message = "DB 계정 비밀번호를 변경했습니다. 기존 VM/요금 데이터는 그대로 유지됩니다."
        next_steps.append(reconnect_hint)

    return LocalDbSetupResult(ok=True, wiped=wiped, message=message, next_steps=next_steps)


# ==========================================================================
# [v4.9] Operations DB 설정 마법사 - "로컬 추가 데이터베이스" / "외부 데이터베이스"
#
# Billing DB와 Operations DB를 분리하는 최초 설정 마법사의 세 번째 선택지(첫째: admin
# 로그인 비밀번호 변경 - 기존 PUT /api/auth/me/password, 둘째: Billing DB 계정 비밀번호/
# 초기화 - 위 apply_local_db_setup)에 해당한다. "로컬 추가 데이터베이스"는 Billing DB가
# 얹혀 있는 것과 같은 PostgreSQL 서버에 새 데이터베이스를 하나 더 만드는 것을 뜻한다
# (새 컨테이너나 SQLite로의 전환이 아니다) - Billing DB 자체가 SQLite 폴백이면 같은
# 서버라는 개념이 없으므로 자연스럽게 "로컬 파일 하나 더 생성"으로 대체된다.
# ==========================================================================


class OperationsDbSetupError(ValueError):
    """Operations DB 설정 처리 중 사용자에게 그대로 보여줄 수 있는(요청이 잘못된) 오류."""


_DB_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


def _validate_new_db_name(name: str) -> str:
    name = (name or "").strip()
    if not _DB_NAME_RE.match(name):
        raise OperationsDbSetupError(
            "데이터베이스 이름은 영문자로 시작하고 영문자/숫자/밑줄(_)만 포함할 수 있습니다 (최대 63자)"
        )
    return name


def _psycopg_connect_autocommit(url: URL, dbname: str):
    """CREATE DATABASE는 트랜잭션 안에서 실행할 수 없어(PostgreSQL 제약), autocommit
    연결을 직접 연다. SQLAlchemy Engine을 거치지 않고 psycopg3로 바로 접속한다 -
    SQLAlchemy의 URL scheme("postgresql+psycopg")은 psycopg.connect()가 그대로
    이해하지 못하므로 host/port/user/password/dbname을 keyword로 풀어서 넘긴다.
    """
    return psycopg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        dbname=dbname,
        autocommit=True,
        connect_timeout=10,
    )


def create_local_additional_operations_database(db_name: str | None = None) -> tuple[str, str]:
    """"로컬 추가 데이터베이스" 선택 처리.

    현재 Billing DB가 PostgreSQL이면, 그 서버 접속 정보(계정/비밀번호)를 그대로 재사용해
    같은 서버에 새 데이터베이스를 CREATE DATABASE로 생성하고 Operations DB 스키마
    (OpsBase.metadata)를 만든 뒤, 그 접속 문자열을 반환한다. Billing DB가 SQLite
    폴백이면 "같은 서버"라는 개념 자체가 없으므로, 대신 billing.db 옆에 operations.db
    라는 두 번째 로컬 파일을 만든다(이 경우도 사용자에게 반환하는 message에 그대로
    설명한다 - 실제로는 "로컬 추가 서버 DB"가 아니라 "로컬 추가 파일"이라는 점을
    숨기지 않기 위함).

    반환값: (operations_database_url, 안내 메시지)
    """
    settings = get_settings()

    if _is_sqlite_url(settings.database_url):
        billing_path = Path(settings.database_url.replace("sqlite:///", "", 1)).resolve()
        ops_path = billing_path.parent / "operations.db"
        ops_url = f"sqlite:///{ops_path}"
        new_engine = create_engine(ops_url, connect_args={"check_same_thread": False})
        try:
            OpsBase.metadata.create_all(bind=new_engine)
        finally:
            new_engine.dispose()
        return ops_url, (
            "현재 Billing DB가 SQLite 로컬 파일이라 '같은 PostgreSQL 서버에 DB 추가'가 불가능합니다 - "
            f"대신 같은 방식의 로컬 파일을 Operations DB로 새로 만들었습니다: {ops_path}"
        )

    billing_url = make_url(settings.database_url)
    new_db_name = _validate_new_db_name(db_name or "vcfbilling_ops")
    if new_db_name == billing_url.database:
        raise OperationsDbSetupError("Billing DB와 같은 이름은 사용할 수 없습니다")

    conn = _psycopg_connect_autocommit(billing_url, dbname=billing_url.database)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (new_db_name,))
            if cur.fetchone() is not None:
                raise OperationsDbSetupError(
                    f"이미 '{new_db_name}' 데이터베이스가 이 PostgreSQL 서버에 존재합니다 - 다른 이름을 지정하세요."
                )
            cur.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(new_db_name)))
    finally:
        conn.close()

    new_url = billing_url.set(database=new_db_name)
    new_engine = create_engine(new_url)
    try:
        OpsBase.metadata.create_all(bind=new_engine)
    finally:
        new_engine.dispose()

    full_url = new_url.render_as_string(hide_password=False)
    return full_url, (
        f"Billing DB와 같은 PostgreSQL 서버에 '{new_db_name}' 데이터베이스를 새로 생성하고 "
        "Operations DB 스키마를 만들었습니다."
    )


def apply_operations_local_setup(req: OperationsDbLocalSetupRequest) -> OperationsDbLocalSetupResult:
    if not req.confirm:
        raise OperationsDbSetupError("confirm=true로 명시적으로 확인해야 실행됩니다")
    url, message = create_local_additional_operations_database(req.db_name)
    masked = make_url(url).render_as_string(hide_password=True)
    return OperationsDbLocalSetupResult(
        ok=True,
        operations_database_url=url,
        operations_database_url_masked=masked,
        message=message,
        next_steps=[
            "위 접속 문자열을 복사해 .env 파일의 OPERATIONS_DATABASE_URL 값으로 채우세요"
            " (SQLite 파일 경로가 반환되었다면 sqlite:/// 그대로 사용하면 됩니다).",
            "docker compose up -d --force-recreate migrate api collector 를 실행해 전환을 반영하세요.",
            "전환 후 이 화면(데이터베이스 메뉴)을 다시 열어 Operations DB가 새 위치로 정상 연결되는지 확인하세요.",
        ],
    )
