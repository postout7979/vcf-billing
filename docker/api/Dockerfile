# [v4.0] API 컨테이너.
#
# 이 컨테이너만 재빌드/재재시작해도 collector/frontend 컨테이너는 그대로 유지된다
# (예: 라우터/과금 로직/PDF 생성만 고쳤을 때는 이 이미지만 다시 빌드하면 됨).
#
# 빌드 컨텍스트는 저장소 루트여야 한다 (docker-compose.yml의 build.context 참고) -
# requirements-api.txt와 app/ 디렉터리를 모두 참조하기 때문.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 컨테이너 자체 헬스체크(HEALTHCHECK)와 docker-compose의 depends_on
# condition: service_healthy에 쓰기 위한 curl만 추가로 설치한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

# static/은 frontend 컨테이너가 서빙하지만, StaticFiles 마운트(app/main.py)가
# 디렉터리 존재를 요구하므로 API 이미지에도 그대로 포함시킨다.
COPY app ./app
COPY static ./static

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

# API_WORKERS: PostgreSQL 전환으로 더 이상 "--workers 1" 제약이 없다(README 참고).
# 기본 2, docker-compose.yml의 environment로 조정 가능.
ENV API_WORKERS=2
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS}"]
