"""FastAPI 애플리케이션 진입점."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.auth import hash_password
from app.collector import run_forever
from app.config import BASE_DIR, get_settings
from app.database import Base, SessionLocal, engine
from app.models import User, UserRole
from app.routers import admin, auth as auth_router, projects, user

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_default_admin()
    collector_task = asyncio.create_task(run_forever())
    logger.info(
        "VCF Billing Portal 기동 완료 (collector_interval=%s분, 연동 계정별 실 연동/샘플 데이터 자동 사용)",
        settings.collector_interval_minutes,
    )
    try:
        yield
    finally:
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
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
