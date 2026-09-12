# VCF Billing Portal — Docker Compose 배포 가이드 (v4.0)

[v4.0]부터 권장 배포 방식은 Docker Compose다. 이전까지의 venv+systemd 단일 프로세스
방식(SQLite, `--workers 1` 고정, 코드 수정마다 전체를 새로 배치하고 서비스 하나를
재시작)은 `legacy/legacy-ubuntu-deploy.md`에 그대로 남아있지만, 새로 설치하는
경우에는 이 문서를 따르는 것을 권장한다.

## 무엇이 달라졌나 (v3.x → v4.0)

- **단일 프로세스 → 4개 컨테이너**: `db`(PostgreSQL) / `migrate`(1회성 스키마 준비) /
  `api`(FastAPI) / `collector`(수집 백그라운드 루프) / `frontend`(Nginx, 정적 파일 +
  `/api/*` 프록시). 예전에는 코드 어디를 고치든 전체를 다시 배치하고 `systemctl
  restart vcf-billing-portal` 한 번으로 프로세스 전체(웹 서버 + 수집기)가 같이
  재시작됐다. 이제는 예를 들어 수집 로직만 고쳤으면 `docker compose up -d --build
  collector`만 실행하면 되고, API/화면은 끊기지 않는다.
- **SQLite → PostgreSQL**: 파일 기반 SQLite 한 개를 단일 워커가 붙잡던 구조였는데,
  이제 컨테이너를 여러 개(게다가 API는 워커 여러 개)로 나누면서 동시 접속이 필요해져
  PostgreSQL로 옮겼다. 기존 SQLite 데이터가 있다면 아래 "기존 데이터 이관" 참고.
- **TLS/외부 노출은 그대로**: 호스트(Ubuntu)의 nginx + certbot 구성은 그대로 쓴다.
  달라진 건 그 nginx가 이제 uvicorn(:8000)이 아니라 Docker Compose의 frontend
  컨테이너(127.0.0.1:8080)로 프록시한다는 점뿐이다 - 이미 발급받은 인증서와 갱신
  훅에는 영향이 없다.

## 0. 사전 준비

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker

# 현재 계정을 docker 그룹에 넣어두면 매번 sudo 없이 docker 명령을 쓸 수 있음
# (적용하려면 재로그인 필요)
sudo usermod -aG docker "$USER"
```

`docker compose version`, `docker version`으로 정상 설치를 확인한다.

## 1. 저장소 배치

[v4.0]부터는 zip이 아니라 Git 저장소로 코드를 관리한다. 전달받은 방식에 맞게 진행:

```bash
# Git 저장소를 직접 전달받은 경우 (권장 - 이후 코드 변경도 git pull로 반영)
sudo mkdir -p /opt/vcf-billing-portal
sudo chown "$USER":"$USER" /opt/vcf-billing-portal
git clone <전달받은 저장소 경로 또는 URL> /opt/vcf-billing-portal
cd /opt/vcf-billing-portal

# 만약 아직 GitHub/GitLab 등 원격 저장소가 없다면, 전달받은 zip(안에 .git 포함)을
# 풀기만 해도 git log/git pull이 그대로 동작한다. 이후 직접 원격을 등록해서 쓰면 됨:
#   git remote add origin <사내 Git 서버 URL>
#   git push -u origin main
```

## 2. 환경설정 (.env)

```bash
cd /opt/vcf-billing-portal
cp .env.example .env
nano .env
```

최소한 아래 값을 반드시 바꾼다 (.env.example의 각 항목 설명 참고):

- `SECRET_KEY` — `openssl rand -hex 32`로 생성한 랜덤 값. **운영 중에는 절대 바꾸지
  말 것** (연동 계정 비밀번호 암호화 키를 겸함).
- `POSTGRES_PASSWORD` — 임의의 강한 비밀번호로 교체.
- `DATABASE_URL` — 위 `POSTGRES_PASSWORD`와 반드시 같은 값으로 맞출 것 (URL 안에
  비밀번호가 그대로 들어간다).

나머지(`COLLECTOR_INTERVAL_MINUTES`, `COLLECT_USAGE_METRICS`,
`ACCESS_TOKEN_EXPIRE_MINUTES`, `POSTGRES_USER`, `POSTGRES_DB`)는 기본값 그대로 둬도
된다.

## 3. 기동

```bash
cd /opt/vcf-billing-portal
docker compose up -d --build
docker compose ps
```

`migrate` 컨테이너는 스키마 생성 + 기본 admin 계정 생성 후 정상 종료(Exit 0)되는 게
맞다 (계속 떠 있는 컨테이너가 아니다). `db`/`api`/`collector`/`frontend`가 모두
`healthy` 또는 `running`이면 정상이다.

```bash
docker compose logs -f api collector      # 기동 로그 확인 ("기동 완료" 문구 확인)
curl -s http://127.0.0.1:8080/api/health   # {"status":"ok"} 응답 확인
```

## 4. 전용 시스템 계정으로 소유권 이전 (선택)

Docker Compose 자체는 root(또는 docker 그룹 계정)로 돌아가지만, 저장소 파일 소유권은
그대로 자신의 계정이어도 무방하다. 다만 여러 사람이 서버를 공유한다면 v3.x 가이드와
동일하게 전용 계정으로 옮기는 것을 권장한다 (`legacy/legacy-ubuntu-deploy.md` 7번
단계와 동일한 이유).

## 5. nginx + TLS로 외부 노출

`nginx-vcf-billing.conf` 사용 - v3.x와 파일은 같지만 내용이 바뀌었다(이제
`127.0.0.1:8080`으로 프록시). `billing.example.com`을 실제 도메인으로 교체.

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp nginx-vcf-billing.conf /etc/nginx/sites-available/vcf-billing-portal
sudo ln -s /etc/nginx/sites-available/vcf-billing-portal /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d billing.example.com
sudo ufw allow 'Nginx Full'
```

이미 v3.x에서 같은 도메인으로 인증서를 발급받아둔 서버를 그대로 업그레이드하는
경우라면, `nginx-vcf-billing.conf`의 `proxy_pass` 줄만 `:8000` → `:8080`으로 바꾸고
`sudo nginx -t && sudo systemctl reload nginx`만 하면 된다 (인증서 재발급 불필요).

## 6. 최종 확인

`https://billing.example.com` 접속 → `admin` / `admin1!2@3#`로 로그인 → 대시보드
렌더링 확인. 이후 절차(비밀번호 변경, 연동 계정 등록, 테넌트/프로젝트 생성)는
README.md "계정 연동", "테넌트/프로젝트 관리"와 동일하다.

## 7. 기존 SQLite 데이터 이관 (v3.x에서 업그레이드하는 경우만)

신규 설치라면 이 단계는 건너뛴다. 기존 v3.x 서버(`/opt/vcf-billing-portal/data/billing.db`)
에서 옮겨오는 경우:

```bash
# 1) 기존 서버에서 SQLite 파일을 새 서버로 복사
scp <기존서버>:/opt/vcf-billing-portal/data/billing.db /opt/vcf-billing-portal/data/billing.db

# 2) PostgreSQL 컨테이너만 먼저 띄우고 스키마를 준비
cd /opt/vcf-billing-portal
docker compose up -d db
docker compose run --rm migrate

# 3) 이관 스크립트 실행 (scripts/migrate_sqlite_to_postgres.py 상단 설명 참고)
docker compose run --rm -v "$(pwd)/data:/app/data:ro" api \
    python scripts/migrate_sqlite_to_postgres.py --sqlite-path /app/data/billing.db

# 4) 나머지 서비스 기동
docker compose up -d
```

이관 후에는 반드시 로그인/테넌트/프로젝트/VM 목록이 예전과 같은지 확인하고, 예전
v3.x systemd 서비스는 중지·비활성화한다 (`sudo systemctl disable --now
vcf-billing-portal`).

## 8. 개별 서비스 재배포 (이번 개편의 핵심)

```bash
git pull                                   # 새 코드 받기
docker compose up -d --build api           # API(라우터/과금/PDF)만 바꼈을 때
docker compose up -d --build collector     # 수집 로직만 바꼈을 때
docker compose up -d --build frontend      # 화면(static/)만 바꼈을 때
docker compose up -d --build               # 여러 서비스가 같이 바뀌었을 때 (변경 없는
                                            # 서비스는 캐시로 스킵됨)
```

`db`는 보통 재빌드할 일이 없다(공식 이미지 그대로 사용). 스키마가 바뀌는 변경(새
테이블/컬럼 추가 등)을 배포할 때는 `docker compose up -d --build migrate api
collector`처럼 migrate도 같이 재실행해 새 스키마를 먼저 반영한 뒤 api/collector를
띄우는 순서를 지킨다.

## 트러블슈팅

- **frontend에서 502 Bad Gateway**: `docker compose logs api`로 API가 정상
  기동했는지 확인. api 컨테이너를 방금 재배포했다면 frontend의 내부 nginx가 Docker
  임베디드 DNS(127.0.0.11)로 매 요청마다 `api` 서비스명을 다시 조회하도록 이미
  설정되어 있어(`docker/frontend/nginx.conf`의 `resolver` 설정) 보통 자동으로
  따라간다 - 그래도 안 되면 `docker compose restart frontend`로 확인.
- **migrate 컨테이너가 실패하고 api/collector가 안 뜸**: `docker compose logs
  migrate`로 원인 확인. 대개 `.env`의 `DATABASE_URL`과 `POSTGRES_USER` /
  `POSTGRES_PASSWORD` / `POSTGRES_DB`가 서로 안 맞는 경우다.
- **db 컨테이너는 healthy인데 migrate가 계속 재시도**: `wait_for_db()`
  (app/database.py)가 최대 30회(약 1분)까지 재시도하므로 대개 자연히 해결된다. 그
  이상 걸린다면 `docker compose logs db`로 PostgreSQL 자체 기동 로그를 확인.
- **`docker compose build`가 이미지 pull 단계에서 멈추거나 실패**: 사내
  방화벽/프록시가 Docker Hub(registry-1.docker.io)를 막고 있는 환경일 수 있다. 사내
  레지스트리 미러가 있다면 `docker/*/Dockerfile`의 `FROM` 줄을 그 미러 주소로
  바꿔서 사용한다.
