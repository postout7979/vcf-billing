# [레거시 - v3.x까지] VCF Billing Portal — Ubuntu 배포 가이드 (venv + systemd)

> **[v4.0] 안내**: v4.0부터는 Docker Compose 기반 배포(`docs/docker-deploy.md`)를
> 권장한다 - PostgreSQL 전환, API/Collector/Frontend 컨테이너 분리로 개별
> 재배포가 가능해졌다. 이 문서는 Docker를 쓸 수 없는 환경(예: 사내 정책상 Docker
> 설치 자체가 막힌 서버)을 위한 폴백으로만 남겨둔다. 아래 내용은 v3.x 시절
> 그대로이며, SQLite/venv/systemd 단일 프로세스 구조를 전제로 한다.

신규 우분투 서버에 처음부터 vcf-billing-portal을 설치·구동하는 전체 절차. 대화 중 실제로 겪은 문제들(reportlab/기타 버전 고정 문제, `externally-managed-environment` 에러, 너무 최신인 시스템 Python에서의 pydantic-core 빌드 실패)을 반영해 정리함.

전제: 우분투 24.04 LTS 이상, sudo 가능한 계정, 신규 서버.

> **참고**: 이 가이드 작성 이후 애플리케이션이 한 차례 더 개편되었습니다 (v3). 관리자 화면이
> 개요/계정 연동/테넌트 관리 3개 메뉴로 나뉘었고, VCF Operations(Aria Operations) 연동 계정은
> 테넌트에 종속되지 않는 **독립된 전역 엔티티**로 "계정 연동" 메뉴에서 등록·관리합니다. 연동
> 계정 하나로 수집한 vCenter→Datacenter→Cluster/VM Folder/VM 전체 인벤토리(및 VM Tag)를
> 바탕으로, 테넌트 하위 프로젝트마다 Cluster/VM Folder/VM Tag를 **다중 선택·조합**해 매칭
> 범위를 지정합니다. 수집 주기도 10분에서 5분으로 변경되었습니다. 설치 절차 자체(패키지/venv/
> systemd/nginx)는 이 개편과 무관하게 동일하지만, 4/5/10번 단계의 내용이 최신 구조에 맞게
> 갱신되었습니다. 최신 기능 설명은 README.md를 참고하세요.

## 0. 시스템 Python 버전 확인 (먼저 할 것)

```bash
python3 --version
```

이 프로젝트(FastAPI 0.115 / pydantic 2.9 / SQLAlchemy 2.0 시절 스택)는 Python 3.11~3.13대에서 검증됨. 우분투 버전에 따라 기본 `python3`가 3.14 이상의 아주 최신 버전일 수 있는데, 이 경우 `pydantic-core`처럼 Rust로 빌드되는 의존성이 해당 파이썬 버전용 prebuilt wheel을 아직 못 구해 소스 빌드를 시도하다 실패할 수 있음 (아래 트러블슈팅 참고). 기본 `python3`가 3.14 이상이면 3번 단계에서 `python3.12`를 별도로 설치해 그걸로 venv를 만드는 쪽을 권장.

## 1. 시스템 업데이트 및 필수 패키지 설치

```bash
sudo apt update && sudo apt upgrade -y

# python3-venv 만으로는 부족할 수 있어 python3.12-venv를 명시적으로 추가 설치
# (우분투에서 ensurepip이 버전별 패키지로 분리되어 있어, 이게 없으면
#  `python3 -m venv`가 조용히 깨지거나 "ensurepip is not available" 에러가 남)
sudo apt install -y python3 python3-venv python3-pip python3-dev build-essential
sudo apt install -y git curl unzip sqlite3 tzdata

# 시스템 기본 python3가 너무 최신(예: 3.14+)이면 검증된 버전을 별도 설치
sudo apt install -y python3.12 python3.12-venv || {
  echo "python3.12 패키지가 기본 저장소에 없으면 deadsnakes PPA 사용:";
  echo "  sudo add-apt-repository ppa:deadsnakes/ppa -y && sudo apt update";
  echo "  sudo apt install -y python3.12 python3.12-venv";
}

# (선택) 시스템 전역 한글 폰트. 앱 자체는 나눔고딕 폰트를 리포 안에 이미 포함해
# PDF에 임베드하므로 필수는 아님 — 서버에서 터미널/기타 도구로 한글을 볼 일이
# 있을 때만 설치
sudo apt install -y fonts-nanum fontconfig
```

## 2. 애플리케이션 배치

```bash
sudo mkdir -p /opt/vcf-billing-portal

# 로컬 PC에서 서버로 zip 전송 (예시)
# scp vcf-billing-portal.zip <user>@<server>:/tmp/

sudo unzip /tmp/vcf-billing-portal.zip -d /tmp/
sudo cp -r /tmp/vcf-billing-portal/* /opt/vcf-billing-portal/
sudo chown -R "$USER":"$USER" /opt/vcf-billing-portal   # 아래 설치 작업 동안만 임시로 본인 계정 소유
```

## 3. Python 가상환경 생성 및 의존성 설치

```bash
cd /opt/vcf-billing-portal

# 시스템 기본 python3가 3.14 이상이면 python3.12를 명시적으로 지정해서 venv 생성
# (기본 python3가 3.11~3.13이면 그냥 python3 -m venv .venv 로도 충분)
python3.12 -m venv .venv
source .venv/bin/activate

# 반드시 아래 확인 후 진행 -- .venv 경로가 찍혀야 정상.
# 시스템 경로(/usr/bin/python3 등)가 나오면 venv가 활성화되지 않은 것이므로
# 다음 단계(pip install)로 넘어가면 "externally-managed-environment" 에러가 남.
which python3
which pip

pip install --upgrade pip
pip install -r requirements.txt   # 모든 패키지가 범위(>=,<) 지정으로 완화되어 있음
deactivate
```

> `python3.12 -m venv .venv`가 `ensurepip is not available`로 실패하면 1번 단계의
> `python3.12-venv`가 설치 안 된 것이니 설치 후 `.venv`를 삭제하고 재생성.
>
> `pip install` 시 `sudo`를 붙이지 말 것 — `sudo`는 환경변수를 초기화해 venv를
> 무시하고 시스템 pip로 들어가 버림.

## 4. 환경설정 (.env)

```bash
cp .env.example .env
nano .env
```

VCF Operations/Aria Operations 연동 자격증명은 `.env`에 넣지 않습니다. 연동 계정은
서버 기동 후 관리자 화면의 **"계정 연동"** 메뉴에서 등록하는 전역(global) 엔티티이며,
테넌트에 종속되지 않습니다 — 계정 하나를 등록해두면 여러 테넌트/프로젝트가 그 인벤토리를
공유해서 씁니다 (아래 10번 및 README "계정 연동" 참고). `.env`에서 바꿔야 하는 항목은
사실상 아래 하나뿐입니다.

- `SECRET_KEY` — 임의의 랜덤 문자열로 교체 (`openssl rand -hex 32`).
  이 값은 JWT 서명뿐 아니라 연동 계정 비밀번호를 암호화하는 키로도
  쓰이므로, **서비스 운영 중에는 절대로 바꾸지 마세요** — 바꾸면 이미 저장된
  모든 연동 계정 비밀번호를 복호화할 수 없게 됩니다 (다시 등록 필요).

나머지(`COLLECTOR_INTERVAL_MINUTES`, `ACCESS_TOKEN_EXPIRE_MINUTES`,
`DATABASE_URL`)는 기본값 그대로 두어도 됩니다. `COLLECTOR_INTERVAL_MINUTES`는
기본값이 `5`(5분 주기)로, 사용자 요구사항에 맞춰 이미 설정되어 있습니다.

## 5. 샘플 데이터 시딩 (선택, 동작 확인용)

```bash
source .venv/bin/activate
python -m app.seed_data --days 14
deactivate
```

모의(mock) 연동 계정 1개, 그 인벤토리(가상 vCenter/Datacenter/Cluster/VM Folder/Tag/VM),
테넌트 2개(노바 사업부, 오리온 사업부)와 그 하위 프로젝트 4개(Cluster 기준/Tag 기준/
VM Folder 기준/Cluster+Tag 조합 기준 각 1개씩 — 여러 매칭 방식을 한 번에 확인할 수 있도록
구성), VM 16대, 관리자 계정(`admin`/`admin1!2@3#`)이 생성됩니다. 이 계정은 서버를 처음
기동할 때(`app/main.py`)도 시딩 여부와 무관하게 자동 생성되므로, 시딩을 건너뛰어도 관리자
로그인 자체는 가능합니다 (다만 조회할 데이터가 없음).

## 6. 수동 실행으로 먼저 확인

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

브라우저에서 `http://<서버IP>:8000` 접속해 로그인 화면이 뜨는지 확인. uvicorn이 뜨는 순간 5분 주기 수집기(collector)도 `app/main.py`의 `lifespan`에서 백그라운드 asyncio task로 자동 시작되므로 별도 cron은 불필요. 확인 후 Ctrl+C, `deactivate`.

## 7. 전용 시스템 계정으로 소유권 이전

```bash
sudo useradd --system --home /opt/vcf-billing-portal --shell /usr/sbin/nologin vcfbilling
sudo chown -R vcfbilling:vcfbilling /opt/vcf-billing-portal
```

## 8. systemd 서비스 등록 (상시 구동)

`legacy/vcf-billing-portal.service` 사용 (`EnvironmentFile=/opt/vcf-billing-portal/.env` 경로 일치 필요). `--workers 1`로 고정된 이유: SQLite 파일 DB라 워커를 여러 개 띄우면 동시 쓰기 시 "database is locked" 발생 가능 — PostgreSQL 등으로 옮기기 전까지는 1 유지.

```bash
sudo cp legacy/vcf-billing-portal.service /etc/systemd/system/vcf-billing-portal.service
sudo systemctl daemon-reload
sudo systemctl enable --now vcf-billing-portal

sudo systemctl status vcf-billing-portal
sudo journalctl -u vcf-billing-portal -f
```

`active (running)` + 로그에 "VCF Billing Portal 기동 완료" 확인.

## 9. nginx + TLS로 외부 노출

`nginx-vcf-billing.conf` 사용 (`billing.example.com`을 실제 도메인으로 교체). PDF 결산서 생성 엔드포인트(`statement.pdf`)는 `proxy_read_timeout`을 120초로 별도로 늘려둠.

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp nginx-vcf-billing.conf /etc/nginx/sites-available/vcf-billing-portal
sudo ln -s /etc/nginx/sites-available/vcf-billing-portal /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d billing.example.com   # HTTP->HTTPS 리다이렉트까지 자동 추가
sudo ufw allow 'Nginx Full'
```

> [v4.0] 주의: 저장소 루트의 `nginx-vcf-billing.conf`는 v4.0(Docker Compose,
> `127.0.0.1:8080`)에 맞춰 갱신되어 있다. 이 레거시(v3.x, venv+systemd) 방식으로
> 배포한다면 이 파일을 그대로 쓰지 말고 `proxy_pass`를 `http://127.0.0.1:8000;`으로
> 바꿔서 사용할 것 (8번 단계에서 uvicorn이 8000번에 바인딩됨).

## 10. 최종 확인 및 이후 업데이트

`https://billing.example.com` 접속 → 기본 관리자 계정(`admin` / `admin1!2@3#`, 서버 최초
기동 시 자동 생성)으로 로그인 → 대시보드(개요) 렌더링 확인. 샘플 데이터를 시딩했다면
데모 테넌트 담당자 계정(`nova-lead@corp.com` / `orion-lead@corp.com`, 비밀번호
`demo1234!`)으로도 로그인해 테넌트 스코프 조회가 되는지 확인할 수 있습니다.

**운영 환경이라면 최초 로그인 직후 admin 비밀번호부터 바꾸세요** (현재 UI에는
비밀번호 변경 화면이 없어 `PUT /api/admin/users/{id}/password` API로 재설정해야
합니다). 실 VCF Operations/Aria Operations 환경과 연동하려면 admin으로 로그인한 뒤:

1. **"계정 연동"** 메뉴에서 연동 계정을 새로 등록 (URL/계정/비밀번호 입력 — 등록 즉시
   백그라운드 수집기가 5분 주기로 인벤토리를 가져오기 시작합니다).
2. 수집이 한 번 이상 돈 뒤 **"테넌트 관리"** 메뉴에서 테넌트를 만들고, 그 하위에
   프로젝트를 생성하면서 방금 등록한 연동 계정의 인벤토리에서 Cluster/VM Folder/
   VM Tag를 원하는 만큼 선택해 매칭 범위를 지정하세요.

(README "계정 연동", "테넌트/프로젝트 관리", "실 VCF Operations 환경 연동 방법" 참고 —
더 이상 `.env`를 건드릴 필요가 없습니다.)

코드 업데이트 시: `/opt/vcf-billing-portal`에 새 파일 반영 → `sudo systemctl restart vcf-billing-portal`.

## 겪었던 문제 (트러블슈팅 메모)

- **`Could not find a version that satisfies the requirement reportlab==5.0.1`**: 서버 pip가 보는 패키지 인덱스(사내 프록시/미러 등)에 아직 최신 릴리스가 동기화되지 않아 발생. reportlab이 쓰는 API는 전부 오래된 기본 기능이라 버전을 엄격히 고정할 필요가 없어 `requirements.txt`를 `reportlab>=4.2,<6.0`으로 완화해 해결.
- **`error: externally-managed-environment`**: venv가 실제로 활성화되지 않은 채(또는 `sudo pip install`로) pip를 실행해서 발생. `python3 -m venv .venv` → `source .venv/bin/activate` → `which python3`로 venv 경로 확인 → `sudo` 없이 `pip install` 순서를 반드시 지킬 것. `python3 -m venv`가 실패하면 `python3.12-venv` 패키지 누락이 원인인 경우가 많음.
- **`pydantic-core` 빌드 중 `the configured Python interpreter version (3.14) is newer than PyO3's maximum supported version`**: 서버의 기본 `python3`가 3.14처럼 아주 최신이라, 당시 고정해둔 `pydantic==2.9.2`(→ 내부적으로 오래된 `pydantic-core`)가 그 파이썬용 prebuilt wheel을 못 구해 Rust 소스 빌드로 들어갔다가, 빌드에 쓰이는 PyO3 버전이 3.14를 아직 지원하지 않아 실패. 두 가지로 대응: (1) `requirements.txt`의 모든 버전을 `==` 정확한 고정 대신 `>=,<` 범위로 완화해 pip가 그 시점에 해당 파이썬용 wheel이 있는 최신 버전을 스스로 고르게 함, (2) 그래도 실패하면 근본적으로 `python3.12`처럼 이 스택이 실제 검증된 좀 더 보수적인 버전을 apt로 별도 설치해 그걸로 venv를 만드는 쪽을 권장 (0번 단계 참고).
