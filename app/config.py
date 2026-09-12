"""
애플리케이션 설정.

환경변수(.env)에서 값을 읽어온다. .env.example 참고.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # VCF Operations/Aria Operations 연동 자격증명은 전역 설정이 아니라, 관리자 화면의
    # "계정 연동" 메뉴에서 독립적으로 등록하는 IntegrationAccount(DB, app/models.py)로
    # 관리한다. 하나의 연동 계정 인벤토리를 여러 Tenant/Project로 나눠 사용할 수 있다.

    # 수집 주기 (분) - 과금 단위(block_minutes)와 항상 일치한다 (app/billing/engine.py)
    collector_interval_minutes: int = 5

    # [v3.7] 실 연동(VCFOpsRestClient)이 VM마다 사용량 가중치용 실사용률(cpu|usage_average/
    # mem|usage_average)을 추가로 조회할지 여부. 이 조회는 VM 대수에 비례하는 API 호출을
    # 1개씩 더 추가하므로(v3.4에서 이미 컨테이너 수 비례로 줄여둔 계층 조회와 달리, 이건
    # VM 프로퍼티 조회처럼 VM당 호출임), 대규모 환경에서 수집 속도가 중요하고 사용량
    # 가중치 과금(RateCard.usage_weight_enabled)을 아예 쓰지 않을 계획이면 false로 꺼서
    # 이 추가 호출 자체를 건너뛸 수 있다. 꺼도 기존 Cluster/Folder/Tag/스펙 수집에는
    # 영향이 없다.
    collect_usage_metrics: bool = True

    # 보안
    secret_key: str = "dev-only-insecure-secret-change-me"
    access_token_expire_minutes: int = 480
    algorithm: str = "HS256"

    # DB
    # [v4.0] Docker Compose 구성에서는 DATABASE_URL 환경변수로 PostgreSQL을 가리키도록
    # 설정한다 (예: postgresql+psycopg://vcfbilling:...@db:5432/vcfbilling). 여기 있는
    # SQLite 기본값은 docker-compose 없이 `uvicorn app.main:app`만으로 빠르게 로컬에서
    # 띄워보는 경우를 위한 폴백이며, 운영 배포에는 사용하지 않는다 (동시 쓰기 시
    # "database is locked" 발생 가능 - README "아키텍처" 참고).
    database_url: str = f"sqlite:///{BASE_DIR}/data/billing.db"

    # [v4.0] Collector(VM 인벤토리/전원상태 수집 백그라운드 루프)를 API 프로세스 안에서
    # 함께 띄울지 여부. Docker Compose 구성에서는 collector가 별도 컨테이너로 분리되어
    # 독립적으로 재시작/재배포되므로 API 서비스는 이 값을 false로 끈다(docker-compose.yml
    # 참고). docker-compose 없이 이 저장소를 그냥 `uvicorn app.main:app`으로만 띄워보는
    # 경우(로컬 빠른 확인 등)를 위해 기본값은 true로 유지한다.
    run_collector_in_process: bool = True

    # 과금 기본 통화
    default_currency: str = "KRW"


@lru_cache
def get_settings() -> Settings:
    return Settings()
