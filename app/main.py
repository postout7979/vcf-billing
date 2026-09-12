"""FastAPI 애플리케이션 진입점."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.bootstrap_db import bootstrap
from app.collector import run_forever
from app.config import BASE_DIR, get_settings
from app.routers import admin, auth as auth_router, projects, user

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # [v4.0] 테이블 생성 + 기본 admin 계정 보장 로직은 app/bootstrap_db.py로 옮겨졌다.
    # Docker Compose 구성에서는 이미 `migrate` 서비스가 API/Collector보다 먼저 이 작업을
    # 끝내두므로, 아래 bootstrap() 호출은 "이미 되어 있으면 아무 것도 안 함" 경로만 타는
    # 안전한 재확인일 뿐이다. docker-compose 없이 이 저장소를 그냥
    # `uvicorn app.main:app`으로 띄워보는 로컬 폴백에서는 이 호출이 유일한 부트스트랩
    # 지점이 된다.
    bootstrap()

    # [v4.0] Docker Compose 구성에서는 collector가 별도 컨테이너(app/collector_main.py)로
    # 분리되어 API와 독립적으로 재시작/재배포된다. RUN_COLLECTOR_IN_PROCESS=false(docker
    # compose 기본값)면 API 프로세스 안에서는 collector 루프를 띄우지 않는다. true(기본값,
    # docker-compose 없이 `uvicorn app.main:app`만 띄우는 로컬 폴백)일 때만 기존처럼
    # 인프로세스로 함께 실행한다.
    collector_task = asyncio.create_task(run_forever()) if settings.run_collector_in_process else None
    logger.info(
        "VCF Billing Portal 기동 완료 (collector_interval=%s분, in_process_collector=%s, 연동 계정별 실 연동/샘플 데이터 자동 사용)",
        settings.collector_interval_minutes,
        settings.run_collector_in_process,
    )
    try:
        yield
    finally:
        if collector_task is not None:
            collector_task.cancel()


app = FastAPI(
    title="VCF Billing Portal API",
    description="VCF Operations 리소스 사용량 기반 사설 클라우드 과금 포탈",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(user.router)
app.include_router(admin.router)
app.include_router(projects.router)


@app.get("/api/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


# 정적 파일(프론트엔드)을 루트에 서빙. 반드시 API 라우터 등록 이후에 마운트한다.
# [v4.0] Docker Compose 구성에서는 실제로는 frontend(Nginx) 컨테이너가 static/을 직접
# 서빙하고 /api/*만 이 API 컨테이너로 리버스 프록시하므로, 이 마운트는 도달하지 않는다.
# 다만 docker-compose 없이 API를 단독으로 띄워 확인하는 경우(로컬 폴백)를 위해 유지한다.
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
