"""[v4.4] 관리자 "시스템 상태" 화면용 조회 로직.

무엇을 보여주는가:
- DB 사용량: 전체 DB 크기, 주요 테이블별 행수/크기, 사용량 데이터(PowerSample) 최초/최근
  시각, 활성 커넥션 수.
- API 프로세스 자기 자신의 리소스: CPU%/메모리(RSS)/가동시간.
- 수집기(collector) 상태: 별도 프로세스/컨테이너라 직접 조회할 수 없으므로, 등록된
  연동 계정들의 last_sync_* 필드(app/models.py IntegrationAccount)를 집계해 "수집이
  정상적으로 계속 돌고 있는가"를 간접적으로 보여준다.

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

from app.config import get_settings
from app.models import (
    IntegrationAccount,
    Project,
    PowerSample,
    RateCardHistory,
    Tenant,
    User,
    VirtualMachine,
)
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
_TABLES: list[tuple[str, type]] = [
    ("virtual_machines", VirtualMachine),
    ("power_samples", PowerSample),
    ("projects", Project),
    ("tenants", Tenant),
    ("users", User),
    ("integration_accounts", IntegrationAccount),
    ("rate_card_history", RateCardHistory),
]


def _db_status(db: Session) -> DbStatusOut:
    settings = get_settings()
    is_sqlite = settings.database_url.startswith("sqlite")

    tables: list[DbTableStatOut] = []
    for name, model in _TABLES:
        row_count = db.scalar(select(func.count()).select_from(model)) or 0
        size_bytes = None
        if not is_sqlite:
            # pg_total_relation_size: 테이블 본체 + 인덱스 + TOAST 포함 크기(바이트).
            size_bytes = db.scalar(text("SELECT pg_total_relation_size(:t)"), {"t": name})
        tables.append(DbTableStatOut(name=name, row_count=row_count, size_bytes=size_bytes))

    active_connections: int | None = None
    if is_sqlite:
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        try:
            total_size = Path(db_path).resolve().stat().st_size
        except OSError:
            total_size = None
    else:
        total_size = db.scalar(text("SELECT pg_database_size(current_database())"))
        active_connections = db.scalar(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        )

    oldest = db.scalar(select(func.min(PowerSample.sampled_at)))
    newest = db.scalar(select(func.max(PowerSample.sampled_at)))

    return DbStatusOut(
        engine="sqlite" if is_sqlite else "postgresql",
        size_bytes=total_size,
        active_connections=active_connections,
        tables=tables,
        oldest_usage_sample_at=oldest,
        newest_usage_sample_at=newest,
    )


def _api_process_status() -> ApiProcessStatusOut:
    mem_rss_mb = _PROCESS.memory_info().rss / (1024 * 1024)
    cpu_percent = _PROCESS.cpu_percent(interval=None)
    uptime_seconds = time.time() - _PROCESS.create_time()
    return ApiProcessStatusOut(cpu_percent=cpu_percent, memory_rss_mb=mem_rss_mb, uptime_seconds=uptime_seconds)


def _collector_status(db: Session) -> CollectorStatusOut:
    settings = get_settings()
    accounts = db.scalars(select(IntegrationAccount)).all()

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


def get_system_status(db: Session) -> SystemStatusOut:
    return SystemStatusOut(
        generated_at=dt.datetime.now(dt.timezone.utc),
        db=_db_status(db),
        api_process=_api_process_status(),
        collector=_collector_status(db),
    )
