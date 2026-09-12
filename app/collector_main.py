"""[v4.0] Collector 독립 프로세스 진입점.

Docker Compose 구성에서 `collector` 컨테이너의 CMD로 실행된다 (python -m
app.collector_main). API 프로세스(app/main.py)와 완전히 분리된 별도 프로세스이므로,
수집 로직만 수정했을 때 API 컨테이너를 재빌드/재시작할 필요 없이 이 컨테이너만
재빌드/재시작하면 된다 (반대도 마찬가지).

app/collector.py의 run_forever()는 원래부터 FastAPI에 의존하지 않는 순수 asyncio
루프였기 때문에, 이 모듈은 DB 접속 대기 후 그 루프를 그대로 호출하기만 하면 된다.
"""
from __future__ import annotations

import asyncio
import logging

from app.bootstrap_db import bootstrap
from app.collector import run_forever
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    logger.info("Collector 프로세스 시작 (interval=%s분)", settings.collector_interval_minutes)
    # [v4.0] Docker Compose 구성에서는 `migrate` 서비스가 이미 테이블 생성/기본 admin
    # 계정 생성을 끝낸 뒤 collector가 뜨므로, 아래 bootstrap() 호출은 "이미 되어 있으면
    # 아무 것도 안 함" 경로만 타는 안전한 재확인이다 (app/bootstrap_db.py 참고).
    bootstrap()
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
