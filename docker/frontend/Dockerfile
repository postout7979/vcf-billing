# [v4.0] Frontend 컨테이너 (정적 파일 서빙 + /api/* 리버스 프록시).
#
# static/(HTML/CSS/JS)만 바꿨을 때 이 컨테이너만 재빌드하면 되고 api/collector는
# 영향받지 않는다. 반대로 api를 재배포해도 이 컨테이너는 그대로 - 새 api 컨테이너의
# IP는 nginx.conf의 resolver 설정이 자동으로 따라간다 (nginx.conf 주석 참고).
#
# 빌드 컨텍스트는 저장소 루트여야 한다 (docker-compose.yml 참고).
FROM nginx:1.27-alpine

COPY docker/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY static /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -q --spider http://localhost/ || exit 1
