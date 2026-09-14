"""[v4.4] 관리자 "시스템 상태" 화면용 조회 로직.

무엇을 보여주는가:
- DB 사용량: Billing DB/Operations DB 각각의 크기, 주요 테이블별 행수/크기, 사용량
  데이터(PowerSample) 최초/최근 시각, 활성 커넥션 수. [v4.9] 두 데이터베이스가 물리적으로
  분리될 수 있게 되면서, 이 화면은 둘을 각각 별도로 보여준다(기본 설정에서는 같은 물리
  DB를 가리켜 두 값이 사실상 같은 DB의 부분집합이 되지만, 어느 쪽 수치인지 항상 명확히
  구분해서 보여준다 - 하나만 조용히 보여주고 나머지를 누락하지 않는다).
- API 프로세스 자기 자신의 리소스: CPU%/메모리(RSS)/가동시간.
- 수집기(collector) 상태: 별도 프로세스/컨테이너라 직접 조회할 수 없으므로, 등록된
  연동 계정들의 last_sync_* 필드(app/models_ops.py IntegrationAccount, Operations DB)를
  집계해 "수집이 정상적으로 계속 돌고 있는가"를 간접적으로 보여준다.

한계 (설계 결정):
- collector/frontend/db 컨테이너 자체의 CPU/메모리는 여기서 보여주지 않는다. api
  컨테이너는 리눅스 네임스페이스로 격리돼 있어 psutil로는 자기 자신의 프로세스만
  보인다. 다른 컨테이너까지 보려면 도커 소켓(/var/run/docker.sock)을 api 컨테이너에
  마운트하고 `docker stats`를 대신 실행해야 하는데, 이는 컨테이너 격리를 깨는
  권한 상승이라 사용자 확인 없이 기본값으로 넣지 않았다 - 원한다면 별도 요청 시
  검토 가능 (claude/vcf-billing-portal-design.md "v4.4 개편" 참고).
- collector의 진짜 "살아있음" 하트비트(app/collector.py의 COLLECTOR_HEARTBEAT_FILE)는
  collector 컨테이너 자신의 로컬 파일이라 api 컨테이너에서 읽을 수 없다. 대신 DB에
  이미 저장돼 있는 연동 계정별 last_sync_at/last_sync_status를 집계해 사용한다 -
  연동 계정이 하나도 없으면(등록 전) 이 지표 자체가 비어 보일 수 있다.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import psutil
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import effective_operations_database_url, get_settings
from app.models import Project, RateCardHistory, Tenant, User
from app.models_ops import IntegrationAccount, PowerSample, VirtualMachine
from app.schemas import (
    ApiProcessStatusOut,
    CollectorStatusOut,
    DbStatusOut,
    DbTableStatOut,
    SystemStatusOut,
)

# psutil의 Process.cpu_percent()는 최초 호출 시 비교 기준점이 없어 항상 0.0을
# 반환한다. 모듈 로드(=api 프로세스 시작) 시점에 한 번 "워밍업" 호출을 해 두면,
# 이후 매 요청마다의 호출은 그 사이 구간의 실제 사용률을 반환한다.
_PROCESS = psutil.Process(os.getpid())
_PROCESS.cpu_percent(interval=None)

# (표시 이름, 모델) - 사용량/과금과 직접 관련된 주요 테이블만 추린다. 전체 테이블을
# 다 보여주면 오히려 신호 대비 잡음이 커진다고 판단.
_BILLING_TABLES: list[tuple[str, type]] = [
    ("projects", Project),
    ("tenants", Tenant),
    ("users", User),
    ("rate_card_history", RateCardHistory),
]
_OPERATIONS_TABLES: list[tuple[str, type]] = [
    ("virtual_machines", VirtualMachine),
    ("power_samples", PowerSample),
    ("integration_accounts", IntegrationAccount),
]


def _db_status(session: Session, database_url: str, tables_spec: list[tuple[str, type]]) -> DbStatusOut:
    is_sqlite = database_url.startswith("sqlite")

    tables: list[DbTableStatOut] = []
    for name, model in tables_spec:
        row_count = session.scalar(select(func.count()).select_from(model)) or 0
        size_bytes = None
        if not is_sqlite:
            # pg_total_relation_size: 테이블 본체 + 인덱스 + TOAST 포함 크기(바이트).
            size_bytes = session.scalar(text("SELECT pg_total_relation_size(:t)"), {"t": name})
        tables.append(DbTableStatOut(name=name, row_count=row_count, size_bytes=size_bytes))

    active_connections: int | None = None
    if is_sqlite:
        db_path = database_url.replace("sqlite:///", "", 1)
        try:
            total_size = Path(db_path).resolve().stat().st_size
        except OSError:
            total_size = None
    else:
        total_size = session.scalar(text("SELECT pg_database_size(current_database())"))
        active_connections = session.scalar(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        )

    return DbStatusOut(
        engine="sqlite" if is_sqlite else "postgresql",
        size_bytes=total_size,
        active_connections=active_connections,
        tables=tables,
    )


def _operations_usage_span(ops_db: Session) -> tuple[dt.datetime | None, dt.datetime | None]:
    oldest = ops_db.scalar(select(func.min(PowerSample.sampled_at)))
    newest = ops_db.scalar(select(func.max(PowerSample.sampled_at)))
    return oldest, newest


def _api_process_status() -> ApiProcessStatusOut:
    mem_rss_mb = _PROCESS.memory_info().rss / (1024 * 1024)
    cpu_percent = _PROCESS.cpu_percent(interval=None)
    uptime_seconds = time.time() - _PROCESS.create_time()
    return ApiProcessStatusOut(cpu_percent=cpu_percent, memory_rss_mb=mem_rss_mb, uptime_seconds=uptime_seconds)


def _collector_status(ops_db: Session) -> CollectorStatusOut:
    settings = get_settings()
    accounts = ops_db.scalars(select(IntegrationAccount)).all()

    total = len(accounts)
    never_synced = sum(1 for a in accounts if a.last_sync_status == "never")
    with_error = sum(1 for a in accounts if a.last_sync_status == "error")

    sync_times = [a.last_sync_at for a in accounts if a.last_sync_at is not None]
    last_sync_at = max(sync_times) if sync_times else None

    seconds_since_last_sync = None
    if last_sync_at is not None:
        now = dt.datetime.now(dt.timezone.utc)
        ref = last_sync_at if last_sync_at.tzinfo else last_sync_at.replace(tzinfo=dt.timezone.utc)
        seconds_since_last_sync = (now - ref).total_seconds()

    return CollectorStatusOut(
        interval_minutes=settings.collector_interval_minutes,
        total_accounts=total,
        accounts_never_synced=never_synced,
        accounts_with_error=with_error,
        last_sync_at=last_sync_at,
        seconds_since_last_sync=seconds_since_last_sync,
    )


def get_system_status(db: Session, ops_db: Session) -> SystemStatusOut:
    """[v4.9] db(Billing DB 세션)/ops_db(Operations DB 세션) 둘 다 필요하다 - 두 DB가
    물리적으로 분리될 수 있어 각자의 세션으로 각자의 통계를 조회해야 하기 때문이다."""
    settings = get_settings()
    oldest, newest = _operations_usage_span(ops_db)

    billing_db_status = _db_status(db, settings.database_url, _BILLING_TABLES)
    billing_db_status.oldest_usage_sample_at = oldest
    billing_db_status.newest_usage_sample_at = newest

    operations_db_status = _db_status(
        ops_db, effective_operations_database_url(settings), _OPERATIONS_TABLES
    )
    operations_db_status.oldest_usage_sample_at = oldest
    operations_db_status.newest_usage_sample_at = newest

    return SystemStatusOut(
        generated_at=dt.datetime.now(dt.timezone.utc),
        db=billing_db_status,
        operations_db=operations_db_status,
        api_process=_api_process_status(),
        collector=_collector_status(ops_db),
    )
