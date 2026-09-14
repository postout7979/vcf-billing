# VCF Billing Portal

VCF Operations에서 수집한 VM 리소스 사용량을 기반으로,
퍼블릭 클라우드 사업자와 유사한 형태의 **사설 클라우드 과금 포탈**을 제공하는
데모 애플리케이션입니다. 실제 계산서 발행은 범위에서 제외하고, 리소스 사용량과
예상 요금을 조회하는 UI에 집중합니다.

- **연동 계정(IntegrationAccount)**: VCF Operations 접속 정보를
  **독립적으로/전역으로** 등록합니다(특정 Tenant에 종속되지 않음). 하나의 연동
  계정이 수집한 vCenter → Datacenter → Cluster/VM Folder → VM 인벤토리(+VM Tag)를
  여러 Tenant/Project가 나눠서 사용할 수 있습니다.
- **Tenant(테넌트)**: 최상위 조직 단위. 고객사/사업부 등 독립된 조직 하나에
  대응하며, 하위 Project 목록과 사용자 계정을 가집니다. 관리자가 생성/수정/삭제합니다.
- **Project**: Tenant 하위의 과금 단위. **Cluster / VM Folder / VM Tag 중 여러
  종류를 동시에, 각각 여러 값을 다중 선택**해서 매칭 기준으로 삼습니다 — 셋 중
  하나라도 일치하면(OR 매칭) 해당 VM이 이 Project에 매핑됩니다.
- **관리자(admin)**: "계정 연동"(연동 계정 CRUD + 인벤토리 조회), "테넌트 관리"
  (테넌트/프로젝트/사용자 CRUD), "개요"(전체 또는 특정 테넌트 사용량·요금 교차
  확인) 세 메뉴로 구성된 관리자 화면을 사용합니다.
- **일반 사용자**: 본인이 배정된 Tenant 하위 Project들의 사용량과 요금만 조회할
  수 있습니다 (다른 Tenant의 데이터는 접근 불가).
- 과금 단위: VM의 vCPU / vMEM(GB) / vDisk(GB) × **Power-On 상태였던 시간(5분 단위 집계)**

## 기술 스택 선택 이유

- **백엔드: Python 3.11+ + FastAPI**
  VCF Operations는 REST/JSON API를 제공하며, VMware 진영의
  공식 SDK(pyVmomi, vSphere Automation SDK 등)도 Python 우선으로 제공됩니다.
  FastAPI는 Pydantic 기반 타입 검증과 자동 OpenAPI 문서(`/docs`)를 제공해
  과금 API처럼 정확성이 중요한 서비스에 적합합니다.
- **DB: PostgreSQL [v4.0] (SQLAlchemy ORM)**
  Docker Compose로 API/Collector 컨테이너가 분리되고 API도 여러 워커로 뜨면서
  동시 접속이 필요해져 PostgreSQL로 전환했습니다(v3.x까지는 SQLite 파일 하나를
  단일 워커가 붙잡는 구조였습니다). `DATABASE_URL` 설정 하나로 교체 가능하도록
  ORM으로 추상화되어 있어, docker-compose 없이 빠르게 로컬 확인만 할 때는
  SQLite로 자동 폴백됩니다(`app/config.py` 기본값). vCenter/Datacenter/Cluster/
  VM Folder/Tag는 문자열 속성이 아니라 **정규화된 별도 테이블**로 저장하고,
  Project ↔ Cluster/Folder/Tag는 다대다(M2M) 관계로 표현합니다.
- **프론트엔드: Vanilla JS + Chart.js (로컬 번들, 외부 CDN 의존 없음)**
  VCF/사설 클라우드 환경은 인터넷이 차단된 폐쇄망인 경우가 많아, 빌드 도구나
  외부 CDN 없이 동작하도록 순수 HTML/CSS/JS로 구성했습니다.
  (`static/js/vendor/chart.umd.min.js` 로 로컬 번들 포함)
- **인증: bcrypt 비밀번호 해시 + JWT**
  모든 계정(관리자 포함)에 실제 비밀번호 인증을 적용합니다. 비밀번호는
  `bcrypt`로 단방향 해시하여 저장하고(`app/auth.py`), 로그인 성공 시
  JWT 액세스 토큰을 발급합니다.
- **연동 계정 자격증명 암호화: Fernet (cryptography)**
  연동 계정의 VCF Operations 접속 비밀번호는 DB에 평문으로
  저장하지 않고, 앱의 `SECRET_KEY`에서 파생한 키로 Fernet 대칭 암호화하여
  저장합니다(`app/security/crypto.py`). API 응답에도 절대 포함되지 않습니다.

## 배포 아키텍처 (v4.0: Docker Compose 다중 컨테이너)

[v4.0]부터 권장 배포 방식은 Docker Compose다 — venv+systemd로 단일 프로세스를
띄우던 v3.x까지의 방식은 `legacy/legacy-ubuntu-deploy.md`에 폴백으로 남아있다.
전체 절차는 `docs/docker-deploy.md` 참고. 컨테이너 5개로 구성된다:

```
db         PostgreSQL (데이터) — v3.x까지의 SQLite 단일 파일을 대체
migrate    스키마 생성 + 기본 admin 계정 보장 (1회 실행 후 종료)
api        FastAPI 백엔드 (docker/api/, requirements-api.txt)
collector  VM 인벤토리/전원상태 5분 주기 수집 백그라운드 프로세스
           (docker/collector/, requirements-collector.txt — fastapi/uvicorn/
           reportlab 등 API 전용 패키지가 없는 가벼운 이미지)
frontend   Nginx: static/ 정적 파일 서빙 + /api/*를 api 컨테이너로 리버스 프록시
           (docker/frontend/) — TLS 종료는 그대로 호스트 nginx+certbot이 담당
```

각 서비스를 독립적으로 재빌드/재배포할 수 있다 (`docker compose up -d --build
api`처럼 하나만). 예를 들어 수집 로직만 고쳤으면 `collector`만 재배포하면 되고
화면/API는 끊기지 않는다. 코드 공유는 하나의 `app/` 패키지를 그대로 두고
Dockerfile마다 필요한 부분만 설치하는 방식이다 — `app/collector.py`의
`run_forever()`는 원래부터 FastAPI에 의존하지 않는 순수 asyncio 루프였기 때문에
`app/collector_main.py`가 그대로 재사용한다. 스키마 생성/기본 admin 계정 생성은
`app/bootstrap_db.py`에 모아서 `migrate` 컨테이너가 딱 한 번 실행하는데, 이는
API를 여러 워커로, collector까지 별도 프로세스로 띄우면서 여러 프로세스가 동시에
테이블/기본 admin 계정을 만들려다 부딪히는 경쟁 상태를 막기 위함이다.

## 아키텍처 요약

```
app/
  integrations/         VCF Operations 연동 어댑터 (인터페이스 + Mock/Real 구현)
    base.py               VCFOpsClient 추상 인터페이스, VMSnapshot(vCenter~Folder 계층 + Tag 포함)
    mock_client.py         샘플 데이터 클라이언트 (연동 계정의 is_mock=True일 때 사용)
    vcf_ops_client.py      실 VCF Operations REST API 클라이언트
    __init__.py            build_client_for_integration_account(account) — is_mock 여부에 따라
                            Mock/Real 클라이언트를 선택 생성
  security/
    crypto.py              연동 계정 비밀번호 암호화/복호화 (Fernet, SECRET_KEY 기반)
    passwords.py           [v4.0] bcrypt 비밀번호 해시/검증 (FastAPI/JWT 의존성 없음 -
                            collector 컨테이너의 app/bootstrap_db.py에서도 쓰기 위해
                            app/auth.py에서 분리. app/auth.py가 재노출하므로 기존
                            호출부는 변경 없음)
  collector.py           연동 계정별로 5분 주기 vCenter~VM 인벤토리/전원상태를 수집해
                         계층 테이블(VCenter/Datacenter/Cluster/VMFolder/Tag)을 업서트하고
                         PowerSample을 적재한다. 모든 계정의 수집이 끝나면
                         recompute_project_assignments()가 전체 VM의 project_id를
                         모든 Project의 Cluster/Folder/Tag 다중 선택 기준(OR 매칭)으로
                         전역 재계산한다 (한 계정의 연동 실패가 다른 계정 수집을 막지
                         않도록 예외 격리). [v4.0] 매 사이클 종료 시 하트비트 파일을
                         touch해 Docker 컨테이너 HEALTHCHECK가 읽는다.
  collector_main.py      [v4.0] collector 컨테이너의 진입점 (`python -m
                         app.collector_main`). run_forever()가 원래 FastAPI에
                         의존하지 않는 순수 asyncio 루프였기 때문에 그대로 재사용.
  bootstrap_db.py         [v4.0] 테이블 생성 + 기본 admin 계정 보장. Docker Compose의
                         migrate 컨테이너가 API/Collector보다 먼저 1회 실행해,
                         여러 프로세스가 동시에 스키마/기본 계정을 만들려다 부딪히는
                         경쟁 상태를 없앤다.
  billing/
    engine.py             순수 과금 계산 로직 (수집 간격 block_minutes 블록 단가 계산,
                            기본값은 collector_interval_minutes=5)
    aggregator.py          DB 조회 + engine 결합 -> 프로젝트/기간별 사용량·요금 결과
                            (tenant_id로 필터링 가능)
    statement_pdf.py       월간 결산서 PDF 생성 (테넌트명 포함)
  routers/               FastAPI 라우터 (auth / user(me) / admin / projects)
  models.py              IntegrationAccount / VCenter / Datacenter / Cluster / VMFolder /
                         Tag / Tenant / Project / VirtualMachine / PowerSample / RateCard / User
  schemas.py, database.py, config.py, auth.py, main.py
  seed_data.py           데모 연동 계정 1개(실제 수집 경로로 인벤토리 생성) + 테넌트 2개 ×
                         프로젝트 2개(각기 다른 매칭 기준 조합) + 과거 전원이력 + 계정 시딩
static/                 프론트엔드 (index.html, css/style.css, js/app.js) - [v4.0]
                        Docker Compose 배포에서는 frontend(Nginx) 컨테이너가 직접 서빙
docker/                 [v4.0] api/collector/frontend 각각의 Dockerfile
docker-compose.yml      [v4.0] db/migrate/api/collector/frontend 5개 서비스 정의
requirements-api.txt, requirements-collector.txt   [v4.0] 컨테이너별 최소 의존성
                        (requirements.txt는 venv 레거시 배포용 통합본으로 유지)
docs/docker-deploy.md   [v4.0] Docker Compose 배포 전체 절차 (신규 설치 기준)
k8s/                    [v4.11] Kubernetes 배포용 순수 YAML 매니페스트 (00~07번)
docs/k8s-deploy.md      [v4.11] Kubernetes 배포 전체 절차
legacy/                 [레거시] v3.x venv+systemd 배포 절차와 systemd 유닛
```

**연동 지점이 분리되어 있습니다.** `app/integrations/base.py`의
`VCFOpsClient` 인터페이스 하나(`list_vm_snapshot()`)만 구현하면 되고,
billing 엔진/수집기/API는 이 인터페이스에만 의존합니다. 연동 계정의
`is_mock=True`(데모/시딩 전용, 관리자 API로는 설정 불가)이면
`MockVCFOpsClient`(샘플 데이터)가 자동으로 사용되고, 그 외에는 저장된
접속정보로 실 `VCFOpsRestClient`(`app/integrations/vcf_ops_client.py`)가
생성됩니다.

`VCFOpsRestClient`는 더 이상 스텁이 아니라 VCF Operations
표준 Suite API로 실제로 동작합니다 — 인증 토큰 발급/자동 재발급(401 시
재시도), VM 리소스 목록 페이징 조회, `relationships`(상위 관계) API를 타고
올라가며 vCenter → Datacenter → Cluster/VM Folder 계층을 구성(중첩 폴더의
전체 경로까지 조합), vCPU/vMEM/vDisk 스펙 및 VM Tag(프로퍼티 기반) 조회,
TLS 인증서 미검증(자체서명 인증서) 연결까지 구현되어 있습니다. 다만
리소스 종류 이름과 프로퍼티 키(`vcf_ops_client.py` 상단의 `RESOURCE_KIND_*`/
`PROP_*`/`TAG_PROPERTY_PREFIX` 상수)는 VCF Operations의
버전·관리팩 설정에 따라 다를 수 있어, 실 환경에 붙인 뒤 `scripts/
inspect_integration_account.py`로 실제 값을 확인하고 필요시 상수만 조정하면
됩니다 (아래 "실 VCF Operations 환경 연동 방법" 참고).

## 기본 관리자 계정

서버를 처음 기동하면(`app/main.py`의 `lifespan`) 아래 계정이 자동으로
생성됩니다. 이미 `admin` 계정이 있으면 건드리지 않습니다.

```
아이디: admin
비밀번호: admin1!2@3#
```

**운영 환경에 배포한다면 최초 로그인 직후 반드시 비밀번호를 변경하세요**
(현재 UI에는 비밀번호 변경 화면이 없으므로, `PUT /api/admin/users/{id}/password`
API를 직접 호출하거나 DB에서 `hash_password()`로 재해시해 넣어야 합니다).

## 관리자 화면 구성

관리자로 로그인하면 상단에 3개 메뉴 탭이 나타납니다.

### 1. 개요

전체(또는 테넌트 필터로 선택한 특정) 테넌트의 프로젝트별 사용량/요금을
KPI·차트·표로 확인하고, 프로젝트별 단가(RateCard)를 설정합니다. 행을 클릭하면
VM별 상세 내역(드릴다운)을 볼 수 있습니다.

### 2. 계정 연동

VCF Operations 연동 계정을 **테넌트와 무관하게 독립적으로**
등록/수정/삭제합니다.

- **"+ 새 연동 계정"** — 종류(VCF Operations), 표시 이름,
  Base URL, 사용자명/비밀번호, authSource, TLS 검증 여부를 입력해 등록합니다.
  비밀번호는 항상 암호화되어 저장되며, 보안상 기존 값을 화면에 다시 보여주지
  않으므로 수정 시 비워두면 기존 비밀번호가 유지됩니다. **등록(또는 URL/계정/
  비밀번호 등 접속 정보가 바뀐 수정) 즉시 서버가 한 번 연결을 시도**해서 그
  결과를 바로 알려줍니다 — 정보가 틀렸다면 계정 자체는 그대로 저장된 채
  "연동 실패" 상태로 남고, 실패 사유(토스트 메시지 및 상태 배지의 마우스오버
  툴팁)를 보고 정보를 고쳐 다시 저장하거나 "가져오기" 버튼으로 재시도하면 됩니다.
- **연동 상태 배지** — 목록의 "연동 상태" 열에 계정별로 현재 상태가 표시됩니다.
  - `✓ 연동됨` (초록) — 마지막 연동이 성공. 마우스오버하면 마지막 수집 시각과
    VM 대수가 보입니다.
  - `✗ 연동 실패` (빨강) — 마지막 연동이 실패. 마우스오버하면 실패 사유가
    보입니다.
  - `연동 대기` (주황) — 아직 한 번도 연동을 시도한 적이 없음(이론상 API로
    직접 만든 경우가 아니면 거의 발생하지 않습니다 — 화면에서 만들면 등록 시
    바로 연동을 시도하기 때문입니다).
- **"가져오기"** — 5분 주기 자동 수집을 기다리지 않고, 그 자리에서 즉시 연결을
  시도해 최신 인벤토리(vCenter~VM, vCPU/vMEM/vDisk, Tag)를 가져옵니다. 연결
  테스트를 겸하므로, 자격증명을 고친 뒤 바로 확인하고 싶을 때도 이 버튼을
  누르면 됩니다. 이 계정의 인벤토리 패널을 이미 열어둔 상태라면 가져온 직후
  화면도 함께 갱신됩니다.
- **"인벤토리"** — 해당 계정이 수집한 vCenter → Datacenter → Cluster/VM Folder
  계층과 VM Tag 목록을 트리 형태로 확인합니다 (5분 주기로 자동 수집·갱신, 또는
  "가져오기"로 즉시 갱신).
- **"삭제"** — 계정과 함께 수집된 인벤토리(vCenter~VM)가 모두 삭제됩니다. 이
  인벤토리를 매칭 기준으로 쓰던 Project는 해당 Cluster/Folder/Tag 선택이
  함께 사라지므로, 다른 기준이 남아있지 않다면 그 프로젝트는 더 이상 어떤
  VM도 표시하지 않게 됩니다.

연동 상태는 계정의 `last_sync_status`("never"/"success"/"error") /
`last_sync_at` / `last_sync_error` / `last_sync_vm_count` 필드로 저장되며,
"가져오기" 버튼뿐 아니라 5분 주기 백그라운드 수집기가 각 계정을 수집할 때도
똑같이 갱신됩니다 — 즉 자동 수집이 실패하기 시작해도(예: 운영 중 비밀번호
만료) 화면에서 바로 "연동 실패"로 확인할 수 있습니다.

관련 API: `GET/POST /api/admin/integration-accounts`,
`PUT/DELETE /api/admin/integration-accounts/{id}`,
`POST /api/admin/integration-accounts/{id}/sync` (가져오기),
`GET /api/admin/integration-accounts/{id}/inventory`.

### 3. 테넌트 관리

- **"+ 새 테넌트"** — Key/이름/설명을 입력해 생성합니다. 목록에서 **"수정"**
  으로 이름/설명을 바꾸거나 **"삭제"**로 테넌트와 하위 Project/User를 함께
  삭제할 수 있습니다 (연동 계정/인벤토리는 독립 엔티티이므로 삭제되지 않고
  다른 테넌트가 계속 사용할 수 있습니다).
- **"관리"** 버튼을 누르면 [v3.5부터] 화면 중앙 팝업(모달)으로 상세 관리
  창이 뜹니다(이전에는 테이블 아래로 펼쳐지는 인라인 패널이었습니다).
  - **프로젝트**: 목록 위의 **"+ 프로젝트 추가"** 버튼이 [v3.5] 프로젝트
    생성 팝업을 엽니다 — 연동 계정을 선택하면 그 인벤토리의 Cluster/VM
    Folder/VM Tag 체크박스가 나타나고, 여러 종류를 동시에 여러 값씩 다중
    선택해 프로젝트를 생성합니다(OR 매칭, 최소 1개 필요). 각 행의
    **"수정"**도 같은 팝업을 재사용해(Key 입력란만 숨겨짐) 이름/설명/담당자
    및 매칭 기준을 다시 선택할 수 있고, **"삭제"**로 제거하면 매핑되어 있던
    VM은 매칭이 해제됩니다(다른 프로젝트 기준에 해당하면 그쪽으로 재매핑됨).
  - **사용자 계정**: 이메일/비밀번호/표시 이름을 입력해 이 Tenant 전용 사용자
    계정을 생성합니다. 생성된 계정은 로그인 시 이 Tenant의 Project들만 볼 수
    있습니다.
- 화면 상단의 **테넌트 필터** 드롭다운(관리자 전용, "개요" 탭에 적용)으로
  "전체 테넌트(교차 확인)" 또는 특정 테넌트 하나만 골라 볼 수 있습니다.

관련 API: `GET/POST /api/admin/tenants`, `GET/PUT/DELETE /api/admin/tenants/{id}`,
`GET/POST /api/admin/tenants/{id}/projects`,
`PUT/DELETE /api/admin/tenants/{id}/projects/{project_id}`,
`GET/POST /api/admin/tenants/{id}/users`,
`GET /api/admin/users`, `PUT /api/admin/users/{id}/password`,
`DELETE /api/admin/users/{id}`.

## Project ↔ VM 매핑 방식 (Cluster/VM Folder/VM Tag 다중 선택, OR 매칭)

Project는 Cluster / VM Folder / VM Tag 세 종류의 매칭 기준을 **동시에** 가질
수 있고, 각 종류마다 **여러 값**을 선택할 수 있습니다. VM은 선택된 값 중
**하나라도** 일치하면(OR) 그 Project에 매핑됩니다.

이 재계산은 `app/collector.py`의 `recompute_project_assignments()`가 매 수집
주기(5분)마다 **전체 VM을 대상으로 전역적으로** 수행합니다 (Project가 특정
연동 계정에 종속되지 않으므로, 계정 단위가 아니라 항상 전체를 한 번에
재계산합니다). 매칭 우선순위는 **Cluster 일치 > VM Folder 일치 > VM Tag
일치** 순이며, 같은 Cluster/Folder/Tag를 여러 Project가 동시에 선택한 경우
**id가 가장 작은(=먼저 생성된) Project**가 우선합니다. 어떤 기준에도
해당하지 않는 VM은 "미배정"(project_id = null) 상태로 남습니다.

> 실 VCF Operations 연동 시 vCenter/Datacenter/Cluster/VM
> Folder 계층과 VM Tag를 가져오는 정확한 리소스 종류 이름/프로퍼티 키는
> 환경별로 다를 수 있어, `app/integrations/vcf_ops_client.py` 상단의
> `RESOURCE_KIND_*`/`PROP_*`/`TAG_PROPERTY_PREFIX` 상수로 분리해 두었습니다.
> 코드 자체(인증/페이징/계층 탐색/태그 파싱 로직)는 이미 동작하므로, 실
> 환경에 붙인 뒤 `scripts/inspect_integration_account.py`로 실제 값을 눈으로
> 확인하고 이 상수들이 맞는지만 검증하면 됩니다 (아래 "실 VCF Operations
> 환경 연동 방법" 참고).

## 과금 로직

1. Collector가 `COLLECTOR_INTERVAL_MINUTES`(기본 5분)마다 **연동 계정별로**
   vCenter~VM 인벤토리와 전원상태/스펙을 수집하여 `PowerSample` 1행으로
   적재합니다. 이 1행이 곧 과금 단위(5분 블록) 1개입니다. 한 연동 계정의
   접속이 실패해도 예외가 격리되어 다른 계정의 수집에는 영향을 주지 않습니다.
2. 블록 비용 = `vCPU수 × vcpu_rate_per_hour × (5/60)`
   `+ vMEM(GB) × vmem_rate_per_hour_gb × (5/60)`
   `+ vDisk(GB) × vdisk_rate_per_hour_gb × (5/60)`
   (블록 길이는 `app/billing/engine.py`에서 `collector_interval_minutes`
   기준으로 파라미터화되어 있어, 간격을 바꾸면 자동으로 반영됩니다)
3. Power-Off 상태로 관측된 블록은 과금하지 않습니다.
4. 기간 합계 = 기간 내 모든 Power-On 블록 비용의 합 (`app/billing/engine.py`)

### 사용량 가중치 하이브리드 과금 (선택 기능)

프로젝트별 요금 설정(관리자 화면 "요금 설정" 모달)에서 **"사용량 가중치
반영"**을 켜면, 스펙(vCPU/vMEM 개수·용량) 기준 정액 과금 대신 **실사용률에
비례한 가중치**를 vCPU/vMEM 요금에만 곱해서 계산합니다. 기본값은 꺼짐이며,
꺼진 프로젝트는 기존과 완전히 동일하게 계산됩니다(하위 호환).

- 가중치 = `최소 가중치 + (1 - 최소 가중치) × (사용률 / 100)`, 0~1로 clamp.
  최소 가중치(기본 30%)는 완전 유휴 VM도 최소한의 기본 요금은 부과하기 위한
  바닥값이며, 프로젝트별로 0~100% 사이에서 조정할 수 있습니다.
- 사용률은 Collector가 블록마다 함께 수집하는 `cpu|usage_average`,
  `mem|usage_average`(둘 다 "할당량 대비 사용률 %", VCF Operations Suite API
  기준)를 그대로 사용합니다. vCPU 요금은 CPU 사용률, vMEM 요금은 메모리
  사용률로 각각 독립적으로 가중합니다.
- **vDisk 요금은 가중치를 적용하지 않습니다** (스토리지는 할당된 만큼 항상
  과금하는 것이 일반적인 클라우드 과금 관행과 일치합니다).
- 사용률 값을 구하지 못한 블록(수집 실패, 구형 환경, Power-Off 등)은 가중치
  1.0(기존과 동일한 정액 요금)으로 계산되어 안전하게 폴백합니다.
- 대시보드의 VM별 상세 테이블에는 기간 내 Power-On 블록의 평균 CPU/메모리
  사용률이 "평균 사용률" 열로 함께 표시됩니다(예: `50% / 68%`). 값이 하나도
  없으면 `-`로 표시합니다.
- 실 연동 환경에서는 VM마다 `/stats/latest` 호출이 1회 추가되므로, VM 수가
  매우 많은 환경에서 수집 부하를 줄이려면 `.env`의 `COLLECT_USAGE_METRICS=false`
  로 이 호출 자체를 끌 수 있습니다(끄면 모든 VM의 사용률이 없음으로 처리되어
  가중치 켠 프로젝트도 정액 요금으로 계산됩니다).
- 데모 시드 데이터는 "Nova 운영" 프로젝트에서만 이 기능을 켜서 동작을
  바로 확인할 수 있도록 구성했습니다.

> **데모 단순화 안내**: 현재 구현은 조회 시점의 RateCard(현재 단가)를
> 조회 기간 전체에 일괄 적용합니다. 실 운영에서는 단가 변경 이력
> (`RateCardHistory`, 이미 감사 로그로 적재됨)의 effective 기간을 반영해
> 변경 시점 전/후 블록에 각각 다른 단가를 적용하도록 `aggregator.py`를
> 확장하는 것을 권장합니다. 위의 "사용량 가중치 반영"/"최소 가중치" 설정
> 변경도 `RateCardHistory`에 함께 감사 기록되지만 동일하게 조회 시점 값이
> 기간 전체에 소급 적용되므로, 위 확장을 할 때 함께 반영하는 것이
> 좋습니다. 마찬가지로 `COLLECTOR_INTERVAL_MINUTES`를 운영
> 중 변경하면 과거에 다른 간격으로 적재된 `PowerSample`도 새 간격 기준으로
> 일괄 환산되어 집계됩니다.

## 조회 기간

화면 상단의 기간 선택 드롭다운에서 아래 4가지 방식으로 조회할 수 있습니다.

| 값 | 의미 |
|---|---|
| `7d` | 최근 7일 |
| `30d` | 최근 30일 (기본값) |
| `mtd` | 이번 달 1일 ~ 오늘 (Month-to-date) |
| `month` | 특정 캘린더 월 전체 (예: 2026년 6월 1일 00:00 ~ 7월 1일 00:00, KST 기준). `month` 선택 시 나타나는 월 입력 필드로 조회할 월을 고르며, 데이터가 존재하는 월만 `GET /api/me/months`(일반 사용자) 또는 `GET /api/admin/months`(관리자)로 조회해 입력 범위를 제한합니다. |

관리자는 여기에 더해 **테넌트 필터**(전체/특정 테넌트)를 함께 선택할 수
있습니다. API 레벨에서는 `tenant_id` 쿼리 파라미터로 동일하게 필터링합니다
(`GET /api/admin/overview?period=30d&tenant_id=3`).

## 월간 결산서 PDF export

완료된 캘린더 월을 조회 중일 때("특정 월 조회..." 선택 시) 화면에 **"PDF 결산서
다운로드"** 버튼이 나타납니다 (일반 사용자: 대시보드 상단 / 관리자: 프로젝트
목록 각 행 + VM별 상세 드릴다운). 클릭하면 해당 프로젝트·월의 사용량/요금
결산서를 PDF로 즉시 내려받습니다. 결산서에는 소속 Tenant명이 함께 표시됩니다.

- 엔드포인트: `GET /api/me/projects/{project_id}/statement.pdf?month=YYYY-MM`
  (본인 Tenant 소속 프로젝트만, 다른 Tenant 프로젝트는 404),
  `GET /api/admin/projects/{project_id}/statement.pdf?month=YYYY-MM` (관리자 - 모든 프로젝트)
- 생성 로직: `app/billing/statement_pdf.py` — reportlab으로 화면과 동일한 데이터
  (테넌트명 / 요약 KPI / 적용 단가 / VM별 상세 테이블)를 담은 A4 가로 방향
  PDF를 만듭니다. 한글은 동봉된 나눔고딕 폰트(`app/billing/fonts/`, SIL OFL)를
  임베드해 렌더링하므로 배포 서버에 한글 폰트가 없어도 항상 동일하게 출력됩니다.
- 이 PDF는 **참고용 사용량 결산 자료**이며, 세금계산서 등 법적 효력이 있는 정식
  계산서가 아닙니다 (문서 하단에 항상 명시됩니다).
- 캘린더 월 조회(`period=month`)에서만 제공됩니다. 최근 7일/30일/이번 달(mtd)처럼
  아직 끝나지 않은 기간은 "결산"의 의미가 없어 버튼이 노출되지 않습니다.

## 실행 방법

### Docker Compose (권장, v4.0)

```bash
cp .env.example .env
# SECRET_KEY / POSTGRES_PASSWORD / DATABASE_URL 채우기 (.env.example 주석 참고)

docker compose up -d --build
docker compose exec api python -m app.seed_data --days 14   # 샘플 데이터 시딩(선택)
```

브라우저에서 http://localhost:8080 접속 (frontend 컨테이너). Ubuntu 서버에 실제
배포하는 전체 절차(사전 준비 패키지, nginx+certbot TLS, 개별 서비스 재배포,
기존 SQLite 데이터 이관 포함)는 `docs/docker-deploy.md`를 참고하세요.

### Kubernetes (v4.11, 단일 서버가 아니라 클러스터에 배포하는 경우)

단일 Docker Compose 서버 대신 기존 Kubernetes 클러스터에 배포하고 싶다면
`k8s/` 아래의 순수 YAML 매니페스트(00~07번, Helm/Kustomize 불필요)를 사용하세요.
db/migrate/api/collector/frontend 5개 컴포넌트 구성은 Docker Compose와 동일하며,
이미지도 같은 Dockerfile(`docker/{api,collector,frontend}/Dockerfile`)을 그대로
빌드해서 씁니다. 전체 절차(이미지 빌드/푸시, Secret 설정, 배포 순서, 외부 노출,
기존 배포에서 데이터 이관)는 `docs/k8s-deploy.md`를 참고하세요.

### venv (docker-compose 없이 빠르게 로컬 확인만 하고 싶을 때)

```bash
# 1) 가상환경 및 의존성 설치
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2) 환경설정 - DATABASE_URL 줄을 지우거나 주석 처리하면 SQLite로 자동 폴백됩니다
cp .env.example .env
# SECRET_KEY는 JWT 서명뿐 아니라 연동 계정 비밀번호 암호화 키로도 쓰이므로
# 반드시 충분히 긴 랜덤 값으로 바꾸세요: openssl rand -hex 32

# 3) 샘플 데이터 시딩 (데모 연동 계정 1개, 테넌트 2개, 프로젝트 4개, VM 16대, 최근 14일 전원이력)
python -m app.seed_data --days 14

# 4) 서버 실행 (RUN_COLLECTOR_IN_PROCESS 기본값 true라 collector도 같은 프로세스 안에서 자동 실행)
uvicorn app.main:app --reload --port 8000
```

브라우저에서 http://localhost:8000 접속. API 문서는 http://localhost:8000/docs.
Docker 자체를 쓸 수 없는 서버에 배포해야 한다면 `legacy/legacy-ubuntu-deploy.md`
(venv+systemd, v3.x까지의 방식)를 참고하세요.

### 데모 로그인 계정

| 아이디 | 비밀번호 | 역할 | 소속 테넌트 |
|---|---|---|---|
| admin | admin1!2@3# | 관리자 | 전체 테넌트 교차 확인 |
| nova-lead@corp.com | demo1234! | 일반 사용자 | Nova 사업부 (Cluster 기준 프로젝트 + Tag 기준 프로젝트) |
| orion-lead@corp.com | demo1234! | 일반 사용자 | Orion 사업부 (VM Folder 기준 프로젝트 + Cluster·Tag 복합 기준 프로젝트) |

[v4.2] 로그인 화면의 데모 계정 드롭다운은 제거되었습니다 - 위 표의 아이디/
비밀번호를 아이디/비밀번호 입력란에 직접 입력해서 로그인하세요.

## 실 VCF Operations 환경 연동 방법

1. 관리자로 로그인 후 **"계정 연동"** 메뉴에서 "+ 새 연동 계정"으로 VCF
   Operations의 Base URL(예: `https://vrops.corp.local`),
   서비스 계정 사용자명/비밀번호, authSource(로컬 계정이면 `local`, AD 연동
   계정이면 해당 인증소스 이름)를 입력합니다. 사내에서 자체서명 인증서를
   쓰는 어플라이언스라면 **"TLS 인증서 검증" 체크를 해제**하세요 — 인증서
   신뢰 여부만 건너뛸 뿐 연결 자체는 그대로 암호화(HTTPS)됩니다. 비밀번호는
   자동으로 암호화되어 저장됩니다. (이 계정은 특정 테넌트에 종속되지 않는
   독립 엔티티입니다 — 한 번만 등록하면 됩니다.) **저장 즉시 서버가 한 번
   연결을 시도**해 인증부터 인벤토리 수집까지 실제로 되는지 바로 확인합니다.
2. 상태 배지가 `✓ 연동됨`으로 바뀌고 vCenter/VM 개수가 채워지면 그대로 3번으로
   넘어가면 됩니다. 만약 `✗ 연동 실패`라면 배지의 툴팁(또는 등록 시 뜬 토스트
   메시지)에 실패 사유가 그대로 나타납니다.
   - 인증 실패(401 등)·TLS 오류·API 경로 자체를 못 찾는 오류(404 등)라면
     Base URL/계정정보/TLS 체크박스를 다시 확인하세요.
   - [v3.4] 계층 탐색 방향이 VM에서 위로(PARENT) 올라가는 방식에서
     Datacenter→Cluster→HostSystem→VirtualMachine 순으로 위에서 아래로
     (CHILD) 내려가는 방식으로 바뀌었습니다(실 운영 환경에서 검증된 별도
     PowerShell 연동 도구의 구현 방식을 그대로 채택). VM 단위로 relationships를
     계속 조회하지 않고 Datacenter/Cluster/HostSystem 수만큼만 조회하므로
     대규모 환경에서 더 빠르고, "PARENT 방향에서만 실패하던" 환경 문제도
     함께 해결됩니다. vCenter는 더 이상 relationships로 찾지 않고(일부
     환경은 vCenter를 어댑터 접속 정보로만 노출), 항상 연동 계정의 Base
     URL에서 유도한 값을 그대로 씁니다 — 여러 vCenter를 한 VCF Operations
     인스턴스가 관리한다면 vCenter/스코프별로 연동 계정을 나눠 등록하세요.
   - "VM N대 전부 Datacenter/Cluster/HostSystem 하위 관계(relationships)
     자체를 하나도 받지 못했습니다" 메시지라면, 이름(`RESOURCE_KIND_*`)
     문제가 아니라 relationships 응답의 최상위 JSON 키 자체가
     `resourceList`/`parent`/`parents`/`resourceKeys` 중 어느 것도 아니라는
     뜻입니다. 서버 로그에 `relationships 응답에서 알려진 키를 찾지
     못했습니다` 경고가 함께 남으며, 거기 적힌 실제 최상위 키 이름을
     `vcf_ops_client.py`의 `_RELATIONSHIP_LIST_KEYS`에 추가하면 해결됩니다.
     확실하지 않으면
     `python scripts/inspect_integration_account.py --account-id <id>`를
     실행하세요 — Datacenter/Cluster/HostSystem 각각 하나씩의 CHILD
     relationships 원본 응답 전체를 가공 없이 그대로 출력해주므로 실제
     최상위 키를 눈으로 바로 확인할 수 있습니다.
   - "VM N대 전부 Datacenter->Cluster->HostSystem->VM 하위 관계 체인을
     끝까지 연결하지 못했습니다" 메시지라면(위와 달리 하위 관계 자체는
     받았지만 체인의 어느 단계에서 원하는 종류를 못 찾은 경우), 메시지에
     이 환경에서 실제로 발견된 `resourceKindKey` 목록이 그대로 나열되어
     있습니다 — `app/integrations/vcf_ops_client.py` 상단의
     `RESOURCE_KIND_CLUSTER`/`RESOURCE_KIND_HOST`(그리고 필요하면
     `RESOURCE_KIND_DATACENTER`/`RESOURCE_KIND_FOLDER`) 상수를 그 목록에
     있는 실제 값으로 바꾸면 됩니다(대소문자 차이는 자동 흡수되므로 신경
     쓰지 않아도 됩니다). Folder는 여전히 선택 사항이라 못 찾아도 VM 자체는
     정상 수집됩니다. 더 자세히 보고 싶으면
     `python scripts/inspect_integration_account.py --account-id <id>`로
     Datacenter/Cluster/HostSystem 각 단계의 CHILD 관계 원본과 실제
     `resourceKindKey`를 확인하세요 — 이 값에 맞춰 상수만 고치면 됩니다
     (코드 구조를 바꿀 필요는 없습니다). vCPU/vMEM/vDisk 값이 이상하거나
     태그가 하나도 안 보이면, 같은 출력에서
     `PROP_NUM_CPU`/`PROP_MEM_KB`/`PROP_DISK_KB`/`TAG_PROPERTY_PREFIX`에
     해당하는 실제 프로퍼티 키 이름을 확인해 역시 상수만 조정하세요. 상수를
     고친 뒤에는 서버를 재시작하고 **"계정 연동" 화면에서 해당 계정의
     "가져오기" 버튼**을 눌러 5분을 기다리지 않고 바로 재확인할 수 있습니다.
   - [v3.5] Cluster/Datacenter/HostSystem은 relationships(CHILD)로 정상
     resolve되는데 **VM Folder나 VM Tag만 계속 비어 있다면**, 이 환경이
     Folder를 relationships에 아예 노출하지 않거나 태그를 `summary|tag|...`
     가 아닌 다른 이름(예: "vSphere Tag")의 프로퍼티로 노출하는 경우일 수
     있습니다(사용자가 실제 VCF Operations 콘솔의 VM 상세 화면에서 확인해준
     사례 — "Parent Cluster"/"Parent Datacenter"/"Parent Folder"/"Parent
     Host"/"vSphere Folder"/"vSphere Tag"가 VM 자신의 Properties로 노출되어
     있었습니다). 이제 `VCFOpsRestClient`는 이런 경우를 대비해 VM 자신의
     프로퍼티에서 "Parent Folder"/"vSphere Folder" 계열 프로퍼티 값을 직접
     읽어 Folder를 보완하고, "vSphere Tag" 계열 프로퍼티(세그먼트 형태 또는
     콤마/세미콜론으로 구분된 다중 태그 문자열 모두 지원)를 태그로 함께
     파싱합니다 — 정확한 키 문자열을 몰라도 되도록 키를 정규화해 느슨하게
     찾으므로 별도 설정이 필요 없습니다. relationships 체인이 일부 VM에서만
     끊긴 경우에도 그 VM의 "Parent Cluster"/"Parent Datacenter" 프로퍼티가
     있으면 그 VM을 통째로 건너뛰지 않고 계층을 보완합니다. 여전히 아무
     것도 안 잡히면
     `python scripts/inspect_integration_account.py --account-id <id>`를
     실행하세요 — [v3.5부터] "parent"/"folder"/"tag"/"vcenter"가 포함된
     것으로 보이는 프로퍼티를 개수 제한 없이 먼저 모아서 보여주고, 전체
     프로퍼티 키 목록도 함께 출력하므로 실제 키 이름을 눈으로 바로 확인할
     수 있습니다.
   - VM의 실제 성능 지표(metric/stat)와 프로퍼티를 함께 눈으로 확인해보고
     싶다면(예: 요금에 사용률 기반 항목을 추가할지 검토하는 경우)
     `python scripts/inspect_vm_metrics.py --account-id <id> --show-catalog`를
     실행하세요 — VM 1대(또는 `--vm-name`/`--vm-id`/`--max-vms`로 지정한
     범위)의 전체 프로퍼티와, `/stats/latest`로 조회한 현재 사용 가능한 모든
     metric의 최신 값을 그대로 출력합니다. `--show-catalog`를 주면 metric
     key에 대응하는 사람이 읽을 수 있는 이름/단위도 함께 보여주고,
     `--output-json <path>`로 전체 샘플을 파일로 저장할 수도 있습니다. 이
     스크립트는 현재 수집기(collector)에는 연결되어 있지 않은 순수 조사용
     도구입니다.
3. 한 번 연동되면 이후에는 5분마다 백그라운드 수집기가 자동으로 같은 경로를
   반복 호출하며, **"계정 연동" → "인벤토리"**에서 언제든 수집된 vCenter~VM
   계층과 Tag를 확인할 수 있습니다.
4. **"테넌트 관리"**에서 대상 Tenant를 생성/선택하고, "프로젝트" 폼에서 방금
   등록한 연동 계정을 선택해 Cluster/VM Folder/VM Tag를 원하는 만큼 다중
   선택해 Project를 생성하고 요금 단위를 설정합니다.
5. "사용자 계정" 폼에서 이 Tenant를 사용할 사용자 계정을 생성해 배정합니다.

## 알려진 제한사항 (다음 단계로 고려할 것)

- 비밀번호 변경/재설정 UI가 없습니다. 관리자가 `PUT /api/admin/users/{id}/password`
  API로 재설정해줘야 합니다. 실 서비스 적용 시 사내 SSO/OIDC 연동으로
  `app/auth.py`를 교체하는 것도 고려할 수 있습니다.
- 단가 변경이 조회 기간 전체에 소급 적용됩니다(위 "데모 단순화 안내" 참고).
- 실제 계산서 발행(세금계산서, 결제 연동 등)은 범위에서 제외되어 있습니다.
- VM 스펙 변경 이력은 `PowerSample`에 스냅샷으로 남지만, 집계 로직은
  현재 스펙 기준으로 단순화되어 있습니다.
- [v3.4] `VCFOpsRestClient`는 이제 Datacenter/Cluster/HostSystem 수에만
  비례하는 API 호출로 전체 계층을 구성합니다(VM별 relationships 호출이
  없어짐 — 대규모 환경에서 성능상 이점이 있습니다). VM 자체의 프로퍼티
  조회(스펙/태그)만 여전히 VM 대수만큼 필요합니다. 이 부분을 배치/동시성
  처리로 더 개선할 여지는 남아 있습니다.
- `VCFOpsRestClient`는 표준 Suite API 규격(인증/페이징/relationships 기반
  계층 탐색/프로퍼티 기반 스펙·태그 조회)으로 실제로 동작하지만, 리소스 종류
  이름과 프로퍼티 키(`RESOURCE_KIND_*`/`PROP_*`/`TAG_PROPERTY_PREFIX`)는
  버전·관리팩 설정에 따라 다를 수 있어 실 환경 최초 연동 시 확인이 필요합니다
  (위 "실 VCF Operations 환경 연동 방법" 및 `scripts/
  inspect_integration_account.py` 참고). 이 변동성 자체를 최대한 방어적으로
  흡수합니다: `RESOURCE_KIND_*` 대소문자 차이는 자동으로 흡수되고,
  relationships 응답의 최상위 JSON 키가 `resourceList`가 아니어도
  `parent`/`parents`/`resourceKeys`까지 자동으로 시도합니다. vCenter는
  [v3.4부터] relationships로 아예 찾지 않고 Base URL 기준 값을 항상 그대로
  쓰므로(실 운영 환경에서 검증된 참고 도구와 동일한 방식) vCenter 노출
  여부와 무관하게 안정적입니다. Folder는 여전히 선택 사항이라 못 찾아도
  실패하지 않습니다(Datacenter/Cluster/HostSystem 체인만 필수). 그래도
  100% 실패하면 에러 메시지 자체에 "하위 관계 자체를 못 받았는지" vs
  "체인 중간에서 이름만 다른지"를 구분하고, 후자의 경우 실제 발견된
  `resourceKindKey` 목록까지 그대로 표시하므로 스크립트를 따로 돌리지
  않고도 바로 원인을 특정할 수 있습니다. VM 태그는 vSphere 태그가 vROps
  프로퍼티로 동기화되어 있다는 일반적인 관례를 전제로 파싱하되, [v3.5부터]
  "vSphere Tag"라는 이름의 별도 프로퍼티(세그먼트 형태 또는 콤마/세미콜론
  구분 다중 태그 문자열)로 노출되는 경우도 함께 지원합니다. 그래도 환경에
  따라 태그가 전혀 다른 방식(예: 별도 리소스 종류로 모델링)으로 노출된다면
  `_extract_tags_from_properties()`를 그 방식에 맞게 다시 작성해야 할 수
  있습니다. [v3.5] VM Folder 역시 relationships로 전혀 노출되지 않는
  환경을 대비해, VM 자신의 "Parent Folder"/"vSphere Folder" 프로퍼티 값을
  직접 읽는 보조 경로(`_extract_folder_from_properties()`)를 두었고,
  Datacenter/Cluster relationships가 일부 VM에서만 끊긴 경우에도 VM
  자신의 "Parent Datacenter"/"Parent Cluster" 프로퍼티로 그 VM만 개별
  보완합니다(정확한 프로퍼티 키 문자열은 몰라도 되도록 키 이름을 정규화해
  느슨하게 매칭합니다 — `_find_property_by_name_fragment()` 참고).
  [v3.6] 사용자가 `scripts/inspect_vm_metrics.py`로 실제 환경에서 수집한
  VM 프로퍼티 원본 샘플을 반영해 두 가지를 추가로 보완했습니다: (1) 태그가
  "vSphere Tag" 계열이 아니라 세그먼트 없는 `summary|tag`/`summary|tagJson`
  프로퍼티로 노출되는 환경도 지원하며(`summary|tagJson`은 JSON 배열로 우선
  파싱을 시도), 태그가 없을 때 이 두 프로퍼티가 갖는 문자열 리터럴 "none"은
  실제 태그 이름으로 오인하지 않고 무시합니다. (2) vCenter를 여전히
  기본값은 계정의 Base URL에서 유도하되, VM 자신의 "Parent vCenter"
  프로퍼티(`summary|parentVcenter`)가 있으면 그 값을 우선 사용합니다 -
  VCF Operations 한 인스턴스가 여러 vCenter를 관리하는 환경에서 더
  정확합니다.
  [v3.8] 사용자가 제공한 `sample.json`(실 환경에서 `inspect_vm_metrics.py`로
  수집)의 프로퍼티 값을 실측 대조한 결과, vCPU 수/vDisk 용량/전원 상태
  프로퍼티 키가 세 가지 모두 실제 환경과 달라 각각 vCPU=0, vDisk=0GB,
  전원상태=꺼짐으로 항상 잘못 계산되고 있던 것을 발견해 수정했습니다:
  vCPU는 `config|hardware|num_Cpu`(오타성 불일치) → `config|hardware|numCpu`,
  vDisk는 `config|hardware|diskKB`(이 환경에 존재하지 않는 키) →
  `config|hardware|diskSpace`(이미 GB 단위 — 개별 `virtualDisk:*|configuredGB`
  값들의 합과 일치함을 확인, 이 프로퍼티가 없으면 개별 디스크 값을 직접
  합산하는 대체 경로도 함께 추가), 전원 상태는 `runtime|powerState`(접두어
  누락) → `summary|runtime|powerState`로 바로잡았고, 값 비교도 `"poweredOn"`
  고정 문자열이 아니라 실제 값 `"Powered On"`(공백 포함)까지 대소문자/공백
  차이 없이 인식하도록 정규화했습니다. 같은 라운드에서 화면/PDF 결산서의
  "Power-On 시간" 표시를 소수 시간(예: "123.4h")에서 시/분(예: "123h 24m")
  단위로 바꾸고, PDF 결산서 문구에 하드코딩되어 있던 "10분 단위"라는 문구가
  실제 수집 간격(기본 5분, `COLLECTOR_INTERVAL_MINUTES`)과 어긋나 있던 것도
  함께 발견해 설정값을 그대로 반영하도록 고쳤습니다.
- "TLS 인증서 검증" 체크를 해제하면 인증서 신뢰 여부와 호스트명 검증을
  모두 건너뛰고, 오래된 어플라이언스와의 호환을 위해 암호화 스위트 보안
  등급과 최소 TLS 버전도 낮춰서 연결합니다 — 사내망에 격리된 자체서명
  인증서 어플라이언스용 옵션이며, 인터넷에 노출된 서버에는 권장하지
  않습니다.
- Cluster/VM Folder/VM Tag가 여러 Project에 동시에 선택되어 겹치는 경우,
  먼저 생성된(id가 작은) Project가 우선합니다 — 이 우선순위는 화면에서
  조정할 수 없습니다 (위 "Project ↔ VM 매핑 방식" 참고).
- 프로젝트 생성/수정 화면은 한 번에 하나의 연동 계정 인벤토리만 보여줍니다.
  한 Project가 서로 다른 연동 계정의 Cluster/Folder/Tag를 함께 매칭 기준으로
  쓰는 것은 API 레벨에서는 가능하지만, 그런 프로젝트를 화면에서 수정하면
  현재 화면에 표시되지 않는(다른 계정 소속) 기존 선택이 유지되지 않을 수
  있습니다.
- 연동 계정 암호화 키는 앱의 `SECRET_KEY`에서 파생됩니다. `SECRET_KEY`를
  바꾸면 기존에 저장된 연동 계정 비밀번호를 더 이상 복호화할 수 없으므로,
  운영 중에는 `SECRET_KEY`를 변경하지 마세요 (변경이 꼭 필요하면 모든
  연동 계정의 비밀번호를 다시 입력해야 합니다).
- 스키마 마이그레이션 도구(Alembic 등)가 없고 `app/bootstrap_db.py`의
  `Base.metadata.create_all()`로 테이블을 생성합니다 — 신규 테이블/DB는 자동으로
  생기지만, 기존 테이블에 새 컬럼이 추가된 경우(예: v3.x의 연동 상태 필드
  `last_sync_status` 추가 당시)에는 기존 DB를 그대로 재사용하면 반영되지
  않습니다. 데모 환경이라면 DB를 지우고 재시딩하는 게 가장 간단합니다
  ([v4.0] PostgreSQL 기준: `docker compose down -v`로 `db_data` 볼륨까지 지운
  뒤 다시 `docker compose up -d --build`). 실 데이터가 있는 서버에 스키마
  변경분을 반영해야 한다면 PostgreSQL에 직접 `ALTER TABLE ... ADD COLUMN
  ...`을 실행하세요 — SQLite 시절과 달리 PostgreSQL의 네이티브 ENUM 컬럼
  (`role`, `kind`, `power_state` 등 `Enum(...)` 매핑 컬럼)에 새 값을 추가하는
  경우는 `ALTER TYPE ... ADD VALUE ...`가 별도로 필요하니 주의하세요.
- [v4.0] `db_data`라는 이름의 Docker 볼륨 하나에 PostgreSQL 데이터 전체가
  들어있습니다. 정기 백업(`docker compose exec db pg_dump -U vcfbilling
  vcfbilling > backup.sql` 등)을 별도로 챙겨야 합니다 - Docker Compose 자체는
  볼륨을 자동으로 백업해주지 않습니다.
