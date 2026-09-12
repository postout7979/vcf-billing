# [v4.0] Collector 컨테이너 (VM 인벤토리/전원상태 5분 주기 수집 백그라운드 프로세스).
#
# API 컨테이너와 완전히 분리되어 있어, 수집 로직(app/collector.py,
# app/integrations/*)만 고쳤을 때 이 컨테이너만 재빌드/재시작하면 되고 API는 영향받지
# 않는다. requirements-collector.txt만 설치하므로 fastapi/uvicorn/reportlab 등
# API 전용 패키지는 이 이미지에 없다.
#
# 빌드 컨텍스트는 저장소 루트여야 한다 (docker-compose.yml 참고).
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements-collector.txt .
RUN pip install -r requirements-collector.txt

COPY app ./app
COPY docker/collector/healthcheck.sh /usr/local/bin/collector-healthcheck.sh
RUN chmod +x /usr/local/bin/collector-healthcheck.sh

# HTTP 서버가 아니라 무한 루프 프로세스이므로, "마지막 수집 사이클 이후 경과 시간"을
# 파일 타임스탬프로 남겨 헬스체크한다 (app/collector.py의 run_forever()가 매 사이클
# touch, docker/collector/healthcheck.sh 참고).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/usr/local/bin/collector-healthcheck.sh"]

CMD ["python", "-m", "app.collector_main"]
