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
# postgresql-client: [v4.6] "데이터베이스" 관리자 메뉴의 DB 내보내기(pg_dump) 기능이
# 이 컨테이너 안에서 pg_dump를 직접 실행하므로 필요하다. 베이스 이미지(python:3.12-slim)의
# 기본 apt 저장소에 있는 버전을 그대로 쓴다 - docker-compose.yml의 db 서비스
# (postgres:16-alpine)와 메이저 버전이 정확히 일치하지 않을 수 있지만(예: 클라이언트
# 15, 서버 16), 이 앱의 스키마는 평범한 테이블/enum/FK/인덱스뿐이라 실제로는 문제없이
# 동작한다. [v4.6에서 한 번 시도했다가 되돌림: PGDG(PostgreSQL 공식) 저장소를 추가해
# 서버와 정확히 같은 메이저 버전을 설치하려 했으나, 베이스 이미지의 Debian 코드네임을
# "bookworm"으로 가정해 하드코딩한 것이 실제 배포 환경과 달라("none of the choices are
# installable" apt 오류) 빌드 자체가 깨졌다. 코드네임을 매번 정확히 맞추는 것보다,
# 외부 저장소 없이 베이스 이미지 기본 패키지만 쓰는 쪽이 훨씬 안정적이라고 판단해
# 단순화했다 - 정확한 메이저 버전이 꼭 필요하면 `. /etc/os-release && echo
# $VERSION_CODENAME`으로 실제 코드네임을 확인한 뒤 PGDG 저장소를 직접 추가하는 것을
# 고려할 수 있다.]
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl postgresql-client \
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
