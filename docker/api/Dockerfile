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

# curl: 컨테이너 자체 헬스체크(HEALTHCHECK)와 docker-compose의 depends_on
# condition: service_healthy에 사용.
#
# postgresql-client-16: [v4.6] "데이터베이스" 관리자 메뉴의 DB 내보내기(pg_dump) 기능이
# 이 컨테이너 안에서 pg_dump를 직접 실행하므로 필요하다. python:3.12-slim(Debian
# bookworm) 기본 apt 저장소의 postgresql-client는 15인데, docker-compose.yml의 db
# 서비스는 postgres:16-alpine이라 버전이 안 맞는다 - pg_dump는 자신보다 최신 버전의
# 서버를 온전히 지원하지 않으므로(공식 문서에 명시된 제약), PGDG 공식 apt 저장소를
# 추가해 정확히 서버와 같은 메이저 버전(16)을 설치한다. db 서비스의 이미지 태그를
# 바꾸면 이 줄의 postgresql-client-16도 그에 맞게 바꿔야 한다. 이 저장소 자체가 이
# 세션 환경에서는 도커 빌드를 검증할 수 없어(README/design.md의 "Docker Hub 접근
# 차단" 한계 참고), apt.postgresql.org 접근이 막힌 네트워크라면 이 블록을 지우고
# 일반 postgresql-client(15)로 대체해도 pg_dump 자체는 대개 동작하되 버전 경고가
# 뜰 수 있다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y --auto-remove gnupg \
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
