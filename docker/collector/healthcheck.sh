#!/bin/sh
# [v4.0] collector 컨테이너 HEALTHCHECK.
#
# app/collector.py의 run_forever()가 매 수집 사이클(기본 5분)마다 touch하는
# /tmp/collector_heartbeat 파일의 mtime을 확인해, "그 파일이 (수집 주기 * 3)분보다
# 오래 전에 갱신되었으면" 루프가 멎은 것으로 보고 비정상으로 판정한다.
# 컨테이너 시작 직후에는 아직 첫 사이클이 안 돌았을 수 있으니, 파일이 아예 없으면
# HEALTHCHECK의 start-period(Dockerfile 참고) 안에서는 넘어가고 그 이후에만 실패로 본다.
set -eu

HEARTBEAT_FILE="${COLLECTOR_HEARTBEAT_FILE:-/tmp/collector_heartbeat}"
INTERVAL_MINUTES="${COLLECTOR_INTERVAL_MINUTES:-5}"
MAX_AGE_MINUTES=$((INTERVAL_MINUTES * 3))

if [ ! -f "$HEARTBEAT_FILE" ]; then
    # 아직 첫 수집 사이클 전일 수 있음 - Dockerfile의 start-period가 이 구간을 보호한다.
    exit 0
fi

if find "$HEARTBEAT_FILE" -mmin "-${MAX_AGE_MINUTES}" | grep -q .; then
    exit 0
fi

echo "collector heartbeat is stale (older than ${MAX_AGE_MINUTES} minutes)"
exit 1
