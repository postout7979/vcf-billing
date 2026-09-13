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
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base
import app.models  # noqa: F401  (Base.metadata에 전체 테이블을 등록시키기 위한 import)
from app.schemas import (
    DatabaseOverviewOut,
    DatabaseTableStatOut,
    ExternalDbConnectionRequest,
    ExternalDbMigrateRequest,
    ExternalDbMigrateResult,
    ExternalDbTableResult,
    ExternalDbTestResult,
)


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def get_database_overview(db: Session) -> DatabaseOverviewOut:
    settings = get_settings()
    is_sqlite = _is_sqlite_url(settings.database_url)

    tables: list[DatabaseTableStatOut] = []
    for table in Base.metadata.sorted_tables:
        row_count = db.scalar(select(func.count()).select_from(table)) or 0
        size_bytes = None
        if not is_sqlite:
            size_bytes = db.scalar(text("SELECT pg_total_relation_size(:t)"), {"t": table.name})
        tables.append(DatabaseTableStatOut(name=table.name, row_count=row_count, size_bytes=size_bytes))

    if is_sqlite:
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        try:
            size_bytes = Path(db_path).resolve().stat().st_size
        except OSError:
            size_bytes = None
        server_version = db.scalar(text("SELECT sqlite_version()"))
        return DatabaseOverviewOut(
            engine="sqlite",
            file_path=str(Path(db_path).resolve()),
            server_version=f"SQLite {server_version}" if server_version else None,
            size_bytes=size_bytes,
            tables=tables,
        )

    url = make_url(settings.database_url)
    size_bytes = db.scalar(text("SELECT pg_database_size(current_database())"))
    server_version = db.scalar(text("SHOW server_version"))
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


def migrate_to_external_postgres(source_db: Session, req: ExternalDbMigrateRequest) -> ExternalDbMigrateResult:
    """현재 연결된 DB의 모든 테이블을 외부 PostgreSQL로 그대로 복사한다.

    scripts/migrate_sqlite_to_postgres.py(v4.0, SQLite -> PostgreSQL 1회성 스크립트)와
    같은 원리를 "현재 연결(SQLite든 PostgreSQL이든 무관)-> 임의의 외부 PostgreSQL"로
    일반화한 것. 대상 DB가 비어있지 않으면(PK 충돌 위험) 안전하게 거부한다.
    """
    target_url = _build_target_url(req)
    target_engine = create_engine(target_url, connect_args={"connect_timeout": 10})
    try:
        Base.metadata.create_all(bind=target_engine)

        with target_engine.connect() as check_conn:
            for table in Base.metadata.sorted_tables:
                existing = check_conn.scalar(select(func.count()).select_from(table)) or 0
                if existing > 0:
                    raise ExternalDbNotEmptyError(
                        f"대상 DB의 '{table.name}' 테이블에 이미 {existing}행이 있습니다. "
                        "마이그레이션 대상 PostgreSQL DB는 반드시 비어 있어야 합니다."
                    )

        table_results: list[ExternalDbTableResult] = []
        source_bind = source_db.get_bind()
        with source_bind.connect() as src, target_engine.begin() as dst:
            for table in Base.metadata.sorted_tables:
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
            message=f"{len(table_results)}개 테이블, 총 {total_rows}행을 외부 PostgreSQL로 복사했습니다.",
            tables=table_results,
            database_url=full_url,
            database_url_masked=masked_url,
            next_steps=[
                "위 접속 문자열을 복사해 .env 파일의 DATABASE_URL 값을 이것으로 교체하세요.",
                "docker compose up -d --force-recreate migrate api collector 를 실행해 전환을 반영하세요"
                " (db 컨테이너는 그대로 두어도 됩니다 - 더 이상 이 앱이 쓰지 않을 뿐입니다).",
                "전환 후 이 화면(데이터베이스 메뉴)을 다시 열어 새 DB로 정상 연결되는지 확인하세요.",
                "확인이 끝나면 기존 로컬 DB는 이번 작업으로 전혀 변경되지 않았으므로,"
                " 필요 시 그대로 백업 삼아 보관하거나 정리하면 됩니다.",
            ],
        )
    finally:
        target_engine.dispose()


def export_database_bytes() -> tuple[bytes, str, str]:
    """현재 DB 전체를 백업 파일 바이트로 만들어 (내용, 파일명, media_type)을 반환한다.

    PostgreSQL은 pg_dump로 복원 가능한 평문 SQL 덤프를, SQLite는 sqlite3 백업 API로
    뜬 일관된 스냅샷 파일(.db, 그대로 data/billing.db 자리에 넣으면 복원됨)을 만든다.
    """
    settings = get_settings()
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if _is_sqlite_url(settings.database_url):
        db_path = Path(settings.database_url.replace("sqlite:///", "", 1)).resolve()
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
        return data, f"vcf-billing-sqlite-backup-{timestamp}.db", "application/x-sqlite3"

    pg_dump_path = shutil.which("pg_dump")
    if pg_dump_path is None:
        raise RuntimeError(
            "pg_dump 실행 파일을 찾을 수 없습니다 (api 컨테이너 이미지에 postgresql-client 미설치) - "
            "api 이미지를 최신 Dockerfile로 재빌드한 뒤 다시 시도하세요."
        )
    url = make_url(settings.database_url)
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
    return data, f"vcf-billing-postgres-backup-{timestamp}.sql", "application/sql"
