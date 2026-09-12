"""
실제 VMware VCF Operations / Aria Operations REST API 연동 클라이언트.

인증(토큰 발급/재발급), VM 리소스 목록 조회(페이징), 계층(vCenter/Datacenter/Cluster/
Host/VM Folder) 구성, vCPU/vMEM/vDisk 스펙 조회, TLS 인증서 미검증(자체서명 인증서 등)
연결까지는 표준 Suite API 규격을 기준으로 실제로 동작하도록 구현되어 있습니다.

[v3.4] 계층 탐색 방향을 VM에서 위로(PARENT) 올라가는 방식에서, Datacenter->Cluster->
HostSystem->VirtualMachine 순으로 위에서 아래로(CHILD) 내려가는 방식으로 전면 교체했습니다.
사용자가 실제 운영 환경에서 검증된 PowerShell 연동 도구(VCFOpsApiClient.psm1/
VCFOpsCollector.psm1)를 참고 자료로 제공해 주셨는데, 그 도구는 정확히 이 방향(CHILD)으로
탐색하고 있었고, vCenter는 아예 relationships 리소스로 찾지 않습니다(대신
`/suite-api/api/adapters`로 어댑터 인스턴스 수만 참고하거나, 사용자가 직접 스코프
레이블을 지정). VM 단위로 PARENT 관계를 타고 올라가는 방식이 이 환경에서 계속
실패했던 것도, 실제로는 relationships API가 이 방향/파라미터 조합에서 기대와 다르게
동작했기 때문일 가능성이 높습니다. 이번 버전은 그 검증된 방식을 그대로 채택했습니다:

  1. Datacenter 목록의 CHILD 관계에서 Cluster를 찾아 cluster->datacenter 매핑을 만든다.
  2. Cluster 목록의 CHILD 관계에서 HostSystem을 찾아 host->cluster 매핑을 만든다.
  3. HostSystem 목록의 CHILD 관계에서 VirtualMachine을 찾아 vm->host 매핑을 만든다.
  4. VM Folder는 Datacenter의 CHILD 중 Folder 종류를 재귀적으로 내려가며(Folder는
     중첩 가능) vm->folder 매핑을 만든다(선택 사항 - 못 찾아도 실패 아님).
  5. vCenter는 relationships로 찾지 않고, 참고 자료와 동일하게 이 연동 계정의
     base_url에서 유도한 값을 그대로 사용한다(계정 하나 = vCenter/스코프 하나라는 전제 -
     Aria Operations 한 인스턴스가 여러 vCenter를 관리한다면 계정을 vCenter별로 나눠
     등록하는 것을 권장).

이렇게 하면 API 호출 수도 VM 대수에 비례하지 않고 Datacenter/Cluster/Host 수에만
비례하게 줄어듭니다(대규모 환경에서 성능상 이점도 있음 - 참고 도구의 설계 의도와 동일).

다만 아래는 VCF Operations/Aria Operations의 버전과 관리팩(Management Pack) 설정에
따라 값/응답 형식이 달라질 수 있는 부분이라, 실제 환경에 붙인 뒤 검증이 필요합니다.
  1. 리소스 종류 이름(RESOURCE_KIND_*): 버전에 따라 다를 수 있습니다(대소문자 차이는
     자동으로 흡수됩니다).
  2. VM 스펙/태그 프로퍼티 키(PROP_*, TAG_PROPERTY_PREFIX): 관리팩 버전에 따라
     이름이 다를 수 있습니다.
  3. relationships 응답에서 하위 리소스 목록이 담기는 최상위 JSON 키
     (_RELATIONSHIP_LIST_KEYS): "resourceList"가 아닐 수 있습니다. 이 키 자체가
     안 맞으면 이름을 아무리 맞춰도 하위 리소스를 하나도 못 받아오므로(모든 VM이
     동일하게 실패), list_vm_snapshot()이 이 경우와 "이름만 틀린 경우"를 구분해서
     에러 메시지에 어느 쪽인지 알려줍니다.

`scripts/inspect_integration_account.py`로 실제 환경에 붙여 리소스/프로퍼티/관계
원본 JSON을 직접 찍어보고, 위 상수들이 실제 값과 일치하는지 확인해 조정하세요
(코드 구조를 바꿀 필요 없이 이 파일 상단 상수만 고치면 됩니다). "가져오기"가 실패하면
그 에러 메시지 자체에도 실제로 발견된 리소스 종류 목록이 포함되어 있어, 스크립트를
따로 돌리지 않고도 어떤 값으로 바꿔야 하는지 바로 알 수 있습니다.

참고 엔드포인트 (VMware Aria/vRealize Operations Suite API, 버전별로 세부 응답 형식이
다를 수 있음):
  - POST /suite-api/api/auth/token/acquire                     인증 토큰 발급
  - GET  /suite-api/api/resources?resourceKind=VirtualMachine  VM 리소스 목록 (페이징)
  - GET  /suite-api/api/resources/{id}/properties              리소스 프로퍼티(스펙/전원상태/태그)
  - GET  /suite-api/api/resources/{id}/relationships?relationshipType=CHILD
         해당 리소스의 하위(포함 관계) 리소스 목록. Datacenter/Cluster/HostSystem을
         이 응답을 타고 내려가며 계층을 구성한다 (list_vm_snapshot 참고).
"""
from __future__ import annotations

import json
import logging
import re
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.integrations.base import TagRef, VCFOpsClient, VMSnapshot

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# VCF Operations 프로퍼티/리소스종류 키 (환경에 따라 다를 수 있으니 실제 응답을
# scripts/inspect_integration_account.py 로 확인 후 필요시 조정)
# --------------------------------------------------------------------------

# [v3.8] 사용자가 실제 환경(vcf-ops01)에서 수집한 원본 샘플(sample.json)로
# vCPU/vDisk/전원상태 프로퍼티 키를 실측 검증한 결과, 아래 세 값이 잘못되어 있어
# vCPU가 항상 0으로, vDisk가 항상 0GB로, 전원상태가 항상 "off"로 잘못 계산되고
# 있었다(키 자체가 실제 환경과 달라 .get()이 매번 기본값/None을 반환했음). 실측
# 값으로 교체했다:
#   - config|hardware|num_Cpu(오타성 불일치) -> config|hardware|numCpu
#   - config|hardware|diskKB(실제 환경에 존재하지 않는 키) -> config|hardware|diskSpace
#     (이미 GB 단위 - 아래 _extract_vdisk_gb() 참고, KB 변환 불필요)
#   - runtime|powerState(접두어 누락) -> summary|runtime|powerState, 값도
#     "poweredOn"이 아니라 "Powered On"(공백 포함, 사람이 읽는 형태)으로 옴 - 아래
#     _extract_power_state() 참고
PROP_NUM_CPU = "config|hardware|numCpu"
PROP_MEM_KB = "config|hardware|memoryKB"
PROP_DISK_SPACE_GB = "config|hardware|diskSpace"  # 실측: 이미 GB 단위, 개별 디스크 합계와 일치
PROP_DISK_KB = "config|hardware|diskKB"  # 레거시/일부 환경 전용 최후 대체 키 (실측 샘플엔 없었음)
PROP_GUEST_OS = "config|guestFullName"
PROP_POWER_STATE = "summary|runtime|powerState"
_POWER_STATE_PROPERTY_KEYS = (PROP_POWER_STATE, "runtime|powerState")  # 뒤는 구버전 대비 최후 대체
_VIRTUAL_DISK_CONFIGURED_GB_PATTERN = re.compile(r"^virtualDisk:[^|]+\|configuredGB$")


def _extract_vdisk_gb(properties: dict[str, str]) -> float:
    """VM 총 vDisk 용량(GB)을 추출한다.

    실측 샘플에서 config|hardware|diskSpace 값("274.0")이 그 VM의 개별
    virtualDisk:*|configuredGB 값들(20.0 + 250.0 + 4.0)의 합과 정확히 일치하는
    것을 확인했다 - 이 프로퍼티는 이미 GB 단위이며 KB 변환이 필요 없다. 이
    프로퍼티가 없는 환경을 위해 (1) 개별 virtualDisk 항목 합산 -> (2) 레거시
    diskKB(KB 단위, 실측 샘플엔 없었음) 순으로 대체한다.
    """
    raw = properties.get(PROP_DISK_SPACE_GB)
    if raw not in (None, ""):
        try:
            return round(float(raw), 2)
        except (TypeError, ValueError):
            pass

    total = 0.0
    found_any = False
    for key, value in properties.items():
        if not _VIRTUAL_DISK_CONFIGURED_GB_PATTERN.match(key):
            continue
        try:
            total += float(value)
            found_any = True
        except (TypeError, ValueError):
            continue
    if found_any:
        return round(total, 2)

    raw_kb = properties.get(PROP_DISK_KB)
    if raw_kb not in (None, ""):
        try:
            return round(float(raw_kb) / (1024 * 1024), 2)
        except (TypeError, ValueError):
            pass

    return 0.0


def _extract_power_state(properties: dict[str, str]) -> str:
    """VM 전원 상태를 "on"/"off" 이진값으로 정규화한다.

    실측 샘플에서 실제 값이 "poweredOn"이 아니라 "Powered On"(공백 포함, 사람이
    읽기 좋은 표기)으로 오는 것을 확인했다 - 대소문자/공백 차이를 흡수하기 위해
    값을 소문자화 + 공백 제거해서 "poweredon"과 비교한다. "Powered Off"/
    "Suspended" 등 그 외 값은 모두 "off"로 취급한다(이 앱은 켜짐/꺼짐 이진
    과금만 지원 - Suspended도 과금 대상 아님).
    """
    for key in _POWER_STATE_PROPERTY_KEYS:
        raw = properties.get(key)
        if raw is None:
            continue
        normalized = re.sub(r"[^a-z0-9]", "", str(raw).lower())
        return "on" if normalized == "poweredon" else "off"
    return "off"

# VM에 부여된 vSphere 태그는 vROps/Aria Operations 프로퍼티에
# "summary|tag|<카테고리>|<태그이름>" 형태로 동기화되는 것이 일반적인 관례입니다.
# 이 접두어로 시작하는 프로퍼티를 모두 태그로 간주해서 파싱합니다. 실제 환경의 키
# 형태가 다르면(예: 접두어가 다르거나 category/name이 없는 단일 세그먼트) 이
# 상수와 _extract_tags_from_properties()의 파싱 로직만 조정하면 됩니다.
TAG_PROPERTY_PREFIX = "summary|tag|"
TAG_DEFAULT_CATEGORY = "vSphere Tag"  # 접두어 이후 세그먼트가 1개뿐일 때 쓸 기본 카테고리명

# 계층 탐색(Datacenter->Cluster->HostSystem->VirtualMachine, CHILD 관계)에 쓰는
# resourceKindKey. VMware Management Pack 기준 표준 명칭이며(사용자가 제공한 실제
# 운영 환경용 PowerShell 도구의 $ResourceKind 매핑과 동일), 커스텀/구버전 관리팩에서는
# 다를 수 있습니다. 대소문자 차이는 _HIERARCHY_KIND_ALIASES가 흡수하지만, 이름 자체가
# 다르면 이 상수를 실제 값으로 바꿔야 합니다.
RESOURCE_KIND_VM = "VirtualMachine"
RESOURCE_KIND_DATACENTER = "Datacenter"
RESOURCE_KIND_CLUSTER = "ClusterComputeResource"
RESOURCE_KIND_HOST = "HostSystem"
RESOURCE_KIND_FOLDER = "Folder"

_HIERARCHY_KINDS = (RESOURCE_KIND_DATACENTER, RESOURCE_KIND_CLUSTER, RESOURCE_KIND_HOST, RESOURCE_KIND_FOLDER, RESOURCE_KIND_VM)
# 대소문자만 다른 resourceKindKey("datacenter" vs "Datacenter" 등)는 다른 값으로 취급하지
# 않도록 소문자로 정규화해서 매칭한다.
_HIERARCHY_KIND_ALIASES = {kind.lower(): kind for kind in _HIERARCHY_KINDS}

# relationships 응답에서 하위 리소스 목록이 담기는 최상위 키 이름. Suite API 버전에 따라
# "resourceList" 대신 "parent"/"parents" 등 다른 키를 쓸 수 있어 후보를 순서대로 시도한다
# (이 키 자체가 다르면, 이름이 아니라 이걸 못 찾은 게 원인이므로 전혀 다른 증상 -
# "하위 관계 자체가 하나도 없음" - 으로 나타나며, list_vm_snapshot()이 이를 구분해서 알려준다).
_RELATIONSHIP_LIST_KEYS = ("resourceList", "parent", "parents", "resourceKeys")

_RESOURCE_LIST_PAGE_SIZE = 1000

# --------------------------------------------------------------------------
# [v3.5] VM 프로퍼티에서 직접 상위 계층/태그를 읽는 보조 경로.
#
# 사용자가 실제 VCF Operations 콘솔(VM 상세 > Properties > Summary)에서 직접 확인해
# 준 화면에는, 이 환경이 CHILD relationships를 굳이 타지 않아도 VM 자신의 프로퍼티로
# "Parent Cluster"/"Parent Datacenter"/"Parent Folder"/"Parent Host"/"Parent vCenter"/
# "vSphere Folder"/"vSphere Tag"를 그대로 노출하고 있는 것이 보였다. v3.4의
# CHILD 관계 기반 계층 탐색은 Datacenter/Cluster/HostSystem에는 잘 맞았지만(사용자
# 환경에서 Cluster 정보는 정상적으로 나타남), 이 환경은 Folder를 relationships로
# 노출하지 않는 것으로 보여 v3.4의 _resolve_folders()가 아무것도 찾지 못했고, 태그도
# 기존에 가정했던 "summary|tag|카테고리|이름" 접두어가 아니라 "vSphere Tag" 계열의
# 다른 키를 쓰는 것으로 보인다.
#
# 정확한 프로퍼티 키 문자열(예: "summary|parentFolder")은 관리팩 버전에 따라 다를 수
# 있어 확정하지 않고, 키를 정규화(소문자화 + 영숫자 외 문자 제거)한 뒤 아래 키워드가
# 포함되는지로 관대하게 매칭한다. "Parent Host Version"/"(DEP) Parent Host Version"처럼
# "parenthost"를 포함하지만 실제로는 다른 값(버전 문자열)인 키를 잘못 집지 않도록
# "version"이 포함된 키는 계층 탐색 후보에서 제외한다.
# --------------------------------------------------------------------------
_PARENT_DATACENTER_NAME_FRAGMENTS = ("parentdatacenter",)
_PARENT_CLUSTER_NAME_FRAGMENTS = ("parentcluster",)
_PARENT_FOLDER_NAME_FRAGMENTS = ("parentfolder", "spherefolder")
_PARENT_HIERARCHY_EXCLUDE_FRAGMENTS = ("version",)
_VSPHERE_TAG_NAME_FRAGMENT = "spheretag"

# [v3.6] 사용자가 scripts/inspect_vm_metrics.py로 실제 VCF Operations 환경에서 직접
# 수집한 VM 프로퍼티 원본 샘플을 확인한 결과, "Parent vCenter"는
# "summary|parentVcenter" 프로퍼티로 그대로 노출되고 있었다(계정의 base_url에서 유도한
# 값보다 정확함 - 특히 Aria Operations 한 인스턴스가 여러 vCenter를 관리하는 환경에서).
# 그리고 태그는 "vSphere Tag"(spheretag 포함) 계열이 아니라 "summary|tag"/
# "summary|tagJson"라는 별도 이름의 프로퍼티였고(둘 다 정규화하면 "spheretag"를
# 전혀 포함하지 않아 v3.5의 매칭에 걸리지 않았다), 태그가 없을 때는 "none"이라는
# 문자열 리터럴 값을 갖고 있었다. 샘플 VM 자체는 태그가 없어(둘 다 값이 "none")
# 태그가 있을 때의 정확한 값 형식(JSON 배열 여부 등)까지는 확인하지 못했으므로,
# "summary|tagJson"은 JSON 배열/문자열 두 형태 모두를 시도하는 방어적 파싱을 한다.
_PARENT_VCENTER_NAME_FRAGMENTS = ("parentvcenter",)

# [v4.2] VCF Operations 어플라이언스 자기 자신도 보통 그 vCenter가 관리하는 VM들 중
# 하나로 인벤토리에 함께 나타난다("본인 도메인 자체는 제외"). VM 자신의 게스트
# 호스트명 프로퍼티(관례상 "net|dnsName" 계열)를 이 정규화된 조각으로 느슨하게 찾아,
# 연동 계정의 base_url 호스트명과 비교한다 - 게스트 호스트명을 못 찾은 VM은 vCenter
# 인벤토리상의 VM 이름 자체와도 비교한다(짧은 호스트명을 VM 이름으로 그대로 쓰는
# 환경이 흔하기 때문).
_GUEST_HOSTNAME_NAME_FRAGMENTS = ("dnsname", "guesthostname")
_TAG_NONE_SENTINELS = ("none",)
# 정규화된 키가 이 값과 "정확히" 일치할 때만 태그로 취급한다(포함 여부가 아니라 완전
# 일치) - "tag"라는 단어 자체는 다른 무관한 프로퍼티에도 흔히 등장할 수 있어, 사용자
# 환경에서 실제로 확인된 이 두 키 이외에는 잘못 집지 않도록 보수적으로 매칭한다.
_TAG_BARE_PROPERTY_NORMALIZED_KEY = "summarytag"
_TAG_JSON_PROPERTY_NORMALIZED_KEY = "summarytagjson"

# [v3.7] 사용량 가중치 하이브리드 과금(app/billing/engine.py)의 입력값으로 쓰는
# 실사용률 stat key. 둘 다 "% of 할당량" 단위라 별도 정규화 없이 그대로 0~100 값으로
# 쓸 수 있다 - scripts/inspect_vm_metrics.py로 사용자 실 환경에서 두 key 모두 정상
# 수집되는 것을 확인했다(app/integrations/vcf_ops_client.py v3.6 검증 참고).
_STAT_KEY_CPU_USAGE = "cpu|usage_average"
_STAT_KEY_MEM_USAGE = "mem|usage_average"


def _normalize_property_name(name: str) -> str:
    """프로퍼티 키를 소문자화 + 영숫자만 남기는 방식으로 정규화한다.

    "summary|parentFolder", "Parent Folder", "summary|parent_folder" 등 실제 키 표기가
    무엇이든 "parentfolder"로 동일하게 정규화되어, 정확한 키 문자열을 몰라도 후보
    키워드 포함 여부로 느슨하게 찾을 수 있게 한다.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find_property_by_name_fragment(
    properties: dict, include_fragments: tuple[str, ...], exclude_fragments: tuple[str, ...] = ()
) -> tuple[str, str] | None:
    """정규화된 키에 include_fragments 중 하나라도 포함되고(exclude_fragments는 전혀
    포함되지 않는) 첫 프로퍼티를 (키, 문자열 값) 형태로 반환한다. 값이 비어있으면 건너뛴다.
    """
    for key, value in properties.items():
        norm = _normalize_property_name(key)
        if any(frag in norm for frag in exclude_fragments):
            continue
        if not any(frag in norm for frag in include_fragments):
            continue
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        return key, text
    return None


class VCFOpsIntegrationError(RuntimeError):
    """연동 자체(인증/HTTP)가 아니라, 응답 형식/모델링 불일치로 처리에 실패했을 때 발생시키는 예외.

    예: relationships 응답에서 Cluster/HostSystem에 해당하는 리소스를 찾지 못한 경우.
    보통 RESOURCE_KIND_* 상수가 실제 환경과 다를 때 발생하므로, 메시지에 어떤 상수를
    확인해야 하는지 명시한다.
    """


@dataclass
class IntegrationConnectionInfo:
    """독립 등록된 IntegrationAccount에서 뽑아낸, 클라이언트 생성에 필요한 최소 정보.

    비밀번호는 이미 복호화된 평문이 담기므로, 이 객체를 로그로 남기지 않도록 주의한다.
    """

    base_url: str
    username: str
    password: str
    auth_source: str = "local"
    verify_ssl: bool = True


def _build_ssl_context(verify_ssl: bool) -> ssl.SSLContext | bool:
    """TLS 검증 여부에 따라 httpx에 넘길 verify 값을 만든다.

    verify_ssl=True 면 표준 검증(True)을 그대로 쓴다.

    verify_ssl=False 는 "이 연동 계정은 자체서명 인증서 등 검증 불가능한 인증서를 쓰는
    사내 어플라이언스를 가리킨다"는 관리자의 명시적 선택이므로, 인증서/호스트명 검증을
    끄는 것은 물론, 오래된 VCF Operations/Aria Operations 어플라이언스가 여전히 쓰고
    있을 수 있는 낮은 보안 등급의 암호화 스위트·레거시 TLS 버전과의 협상도 최대한
    허용하도록 SSLContext를 구성한다 (기본 SSLContext는 OpenSSL의 보안 등급 제한 때문에,
    verify=False 를 줘도 이런 구버전 어플라이언스와의 핸드셰이크 자체가 실패하는 경우가
    실제로 있다).
    """
    if verify_ssl:
        return True

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        # 보안 등급을 낮춰 오래된 키 길이/서명 알고리즘의 인증서도 협상 가능하게 한다.
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:  # noqa: BLE001 - 일부 OpenSSL 빌드에서 SECLEVEL=0을 거부할 수 있음
        logger.warning("SSL SECLEVEL=0 설정에 실패했습니다 - 기본 암호화 스위트로 계속 진행합니다")
    if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (ValueError, AttributeError):  # noqa: BLE001 - 플랫폼이 TLSv1까지 못 내릴 수 있음
        pass
    return ctx


class VCFOpsRestClient(VCFOpsClient):
    """VCF Operations/Aria Operations Suite API 를 호출하는 실 연동 구현체."""

    def __init__(self, conn: IntegrationConnectionInfo):
        if not conn.base_url:
            raise ValueError("연동 계정의 base_url이 비어 있습니다. 관리자 화면에서 확인하세요.")
        self._conn = conn
        self._token: str | None = None

        # vCenter는 relationships 리소스로 찾지 않는다(사용자가 제공한 실제 운영 환경용
        # PowerShell 도구도 동일 - vCenter 식별은 /suite-api/api/adapters 어댑터 인스턴스
        # 수 정도만 참고하고, VM 하나하나를 특정 vCenter에 연결짓지는 않는다). 이 연동
        # 계정의 base_url 호스트명을 그대로 vCenter 식별자/이름으로 쓴다 - 계정 하나가
        # 보통 vCenter/스코프 하나에 대응한다는 전제.
        # (Aria Operations 한 인스턴스가 여러 vCenter를 관리하는 멀티-vCenter 구성이라면
        # 이 값이 부정확할 수 있음 - 그런 환경은 연동 계정을 vCenter/스코프별로 나눠
        # 등록하는 것을 권장한다.)
        host = urlparse(conn.base_url).hostname or conn.base_url
        self._fallback_vcenter_id = f"fallback-vcenter:{host}"
        self._fallback_vcenter_name = host

        # [v4.2] 이 계정 자신의 VCF Operations 어플라이언스를 인벤토리에서 제외하기
        # 위한 식별자. FQDN 전체와, 흔히 VM 이름으로 그대로 쓰이는 짧은 호스트명(첫
        # 라벨) 둘 다와 비교한다 - 예: base_url이 https://vcf-ops.corp.local 이면
        # "vcf-ops.corp.local"과 "vcf-ops" 둘 다 자기 자신으로 취급한다.
        own_host = (host or "").strip().lower()
        self._own_host = own_host
        self._own_host_short = own_host.split(".")[0] if own_host else ""

        if not conn.verify_ssl:
            logger.warning(
                "연동 계정(%s)이 TLS 인증서 검증을 비활성화한 상태로 연결합니다 - "
                "자체서명 인증서 등 신뢰할 수 없는 인증서도 그대로 수락합니다.",
                conn.base_url,
            )
        self._client = httpx.Client(
            base_url=conn.base_url,
            verify=_build_ssl_context(conn.verify_ssl),
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    # -- 인증 -----------------------------------------------------------
    def _authenticate(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        resp = self._client.post(
            "/suite-api/api/auth/token/acquire",
            json={
                "username": self._conn.username,
                "password": self._conn.password,
                "authSource": self._conn.auth_source,
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        self._token = resp.json()["token"]
        return self._token

    def _headers(self, force_reauth: bool = False) -> dict:
        return {
            "Authorization": f"vRealizeOpsToken {self._authenticate(force=force_reauth)}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET 요청 헬퍼. 토큰이 만료되어 401이 오면 재인증 후 한 번 더 시도한다."""
        resp = self._client.get(path, params=params, headers=self._headers())
        if resp.status_code == 401:
            logger.info("인증 토큰이 만료된 것으로 보입니다 - 재인증 후 재시도합니다 (%s)", path)
            resp = self._client.get(path, params=params, headers=self._headers(force_reauth=True))
        resp.raise_for_status()
        return resp

    # -- 리소스 목록 (페이징) ----------------------------------------------
    def _iter_resources(self, resource_kind: str, page_size: int = _RESOURCE_LIST_PAGE_SIZE):
        page = 0
        while True:
            body = self._get(
                "/suite-api/api/resources",
                params={"resourceKind": resource_kind, "page": page, "pageSize": page_size},
            ).json()
            items = body.get("resourceList", [])
            yield from items
            if not items:
                break
            page_info = body.get("pageInfo", {}) or {}
            total = page_info.get("totalCount", len(items))
            page += 1
            if page * page_size >= total:
                break

    # -- 계층(Datacenter->Cluster->HostSystem->VM, CHILD 관계) ---------------
    @staticmethod
    def _extract_relationship_list(body: dict) -> list[dict]:
        """relationships 응답에서 리소스 목록을 뽑아낸다.

        Suite API 버전에 따라 최상위 키가 "resourceList"가 아닐 수 있어(예: "parent",
        "parents") _RELATIONSHIP_LIST_KEYS에 있는 후보를 순서대로 시도한다. 전부
        실패하면(즉 이 서버의 실제 응답 키가 이 후보에도 없으면) 빈 리스트를 반환하되,
        실제 최상위 키 목록을 로그로 남겨 다음에 무엇을 후보에 추가해야 할지 바로
        알 수 있게 한다.
        """
        for key in _RELATIONSHIP_LIST_KEYS:
            value = body.get(key)
            if isinstance(value, list):
                return value
        if body:
            logger.warning(
                "relationships 응답에서 알려진 키(%s)를 찾지 못했습니다 - 이 서버의 실제 최상위 키: %s",
                ", ".join(_RELATIONSHIP_LIST_KEYS),
                sorted(body.keys()),
            )
        return []

    def _get_children(self, resource_id: str, cache: dict[str, list[dict]]) -> list[dict]:
        """resource_id의 CHILD(하위 포함) 관계 리소스 목록을 조회한다(캐시됨).

        [v3.4] 예전에는 VM에서 relationshipType=PARENT로 위로 올라갔지만, 실 운영
        환경에서 이 방향이 계속 실패해(사용자가 제공한, 실제로 검증된 PowerShell
        연동 도구를 참고) Datacenter/Cluster/HostSystem에서 CHILD로 아래로 내려가는
        방식으로 교체했다.
        """
        cached = cache.get(resource_id)
        if cached is not None:
            return cached
        resp = self._get(
            f"/suite-api/api/resources/{resource_id}/relationships",
            params={"relationshipType": "CHILD"},
        )
        children = self._extract_relationship_list(resp.json())
        cache[resource_id] = children
        return children

    def _resolve_folders(self, dc_items: list[dict], cache: dict[str, list[dict]]) -> dict[str, dict]:
        """Datacenter의 최상위 VM Folder부터 재귀적으로 내려가며 vm_id -> Folder 정보
        매핑을 만든다(VM Folder는 여러 겹 중첩될 수 있음).

        Folder는 vCenter 인벤토리에서 선택 사항인 정보라, 이 트리 구성에 실패하거나
        (예: 이 환경에서 Folder 종류 리소스가 relationships에 전혀 노출되지 않음)
        통신 오류가 나도 예외를 던지지 않고 지금까지 모은 매핑만 반환한다 - Folder가
        없어도 Datacenter/Cluster 계층 조회 자체는 여전히 유효하기 때문이다.
        """
        vm_folder: dict[str, dict] = {}

        def walk(folder: dict, path_names: list[str]) -> None:
            try:
                children = self._get_children(folder["identifier"], cache)
            except httpx.HTTPError:
                logger.warning("Folder(%s) 하위 조회 실패, 이 하위는 건너뜁니다", folder.get("identifier"))
                return
            for child in children:
                kind = child.get("resourceKey", {}).get("resourceKindKey")
                canonical = _HIERARCHY_KIND_ALIASES.get(kind.strip().lower()) if kind else None
                if canonical == RESOURCE_KIND_FOLDER:
                    child_name = child.get("resourceKey", {}).get("name", "")
                    walk(child, path_names + [child_name])
                elif canonical == RESOURCE_KIND_VM:
                    vm_folder[child["identifier"]] = {
                        "folder_external_id": folder["identifier"],
                        "folder_path": "/".join(path_names) if path_names else None,
                        "folder_name": path_names[-1] if path_names else None,
                    }

        for dc in dc_items:
            try:
                top_children = self._get_children(dc["identifier"], cache)
            except httpx.HTTPError:
                logger.warning("Datacenter(%s) 하위 Folder 조회 실패, 건너뜁니다", dc.get("identifier"))
                continue
            for child in top_children:
                kind = child.get("resourceKey", {}).get("resourceKindKey")
                canonical = _HIERARCHY_KIND_ALIASES.get(kind.strip().lower()) if kind else None
                if canonical == RESOURCE_KIND_FOLDER:
                    name = child.get("resourceKey", {}).get("name", "")
                    walk(child, [name])

        return vm_folder

    @staticmethod
    def _extract_folder_from_properties(properties: dict) -> dict | None:
        """[v3.5] VM 자신의 프로퍼티에서 Folder 정보를 직접 읽는다.

        _resolve_folders()(Datacenter의 CHILD를 재귀적으로 내려가는 방식)가 이 환경에서
        Folder 종류 리소스를 전혀 못 찾는 경우(관리팩이 Folder를 별도 relationships로
        노출하지 않는 경우)를 위한 대체 경로 - 사용자가 확인해준 것처럼 "Parent Folder"/
        "vSphere Folder" 프로퍼티에 폴더 경로/이름이 이미 문자열로 들어있는 경우가 있다.
        값에 "/"가 있으면 전체 경로("A/B/C")로, 없으면 폴더 이름 하나로 간주한다.
        Folder 리소스의 고유 id는 알 수 없으므로, 같은 경로 문자열이면 항상 같은 값이
        나오도록 경로 자체로 식별자를 만든다(수집 주기마다 안정적으로 같은 VMFolder
        행에 업서트되도록 하기 위함).
        """
        found = _find_property_by_name_fragment(properties, _PARENT_FOLDER_NAME_FRAGMENTS)
        if found is None:
            return None
        _, raw_value = found
        path = raw_value.strip().strip("/")
        if not path:
            return None
        name = path.split("/")[-1] or path
        return {
            "folder_external_id": f"by-path:{path}",
            "folder_path": path,
            "folder_name": name,
        }

    def _is_own_appliance_vm(self, vm_name: str, props: dict) -> bool:
        """[v4.2] 이 VM이 이 연동 계정 자신의 VCF Operations 어플라이언스인지 판단한다.

        게스트 호스트명 프로퍼티(있으면 우선)와 vCenter 인벤토리상의 VM 이름 둘 다를
        후보로 놓고, 계정 base_url의 호스트명(FQDN 또는 짧은 호스트명)과 일치하는지
        본다. own_host를 알 수 없는 상태(base_url 파싱 실패)라면 아무것도 제외하지
        않는다 - 잘못 제외해서 정상 VM이 사라지는 것보다는, 어플라이언스 자신이 한
        대 더 보이는 쪽이 안전하다.
        """
        if not self._own_host:
            return False
        candidates = set()
        if vm_name:
            candidates.add(vm_name.strip().lower())
        hostname_found = _find_property_by_name_fragment(props, _GUEST_HOSTNAME_NAME_FRAGMENTS)
        if hostname_found:
            candidates.add(hostname_found[1].strip().lower())
        if self._own_host in candidates:
            return True
        return bool(self._own_host_short) and self._own_host_short in candidates

    # -- 조회 -------------------------------------------------------------
    def list_vm_snapshot(self) -> list[VMSnapshot]:
        vm_items = list(self._iter_resources(RESOURCE_KIND_VM))
        if not vm_items:
            return []

        # [v3.7] 설정을 VM 루프 시작 전에 한 번만 읽는다(app/config.py의 get_settings()는
        # lru_cache라 비용이 크진 않지만, 루프 안에서 매번 부를 이유가 없다).
        collect_usage_metrics = get_settings().collect_usage_metrics

        # relationships 조회 결과는 한 번의 수집 사이클 안에서 여러 Datacenter/Cluster/
        # Host가 공유되므로, 이 호출 동안만 유효한 캐시로 중복 호출을 없앤다.
        children_cache: dict[str, list[dict]] = {}

        dc_items = list(self._iter_resources(RESOURCE_KIND_DATACENTER))
        cluster_items = list(self._iter_resources(RESOURCE_KIND_CLUSTER))
        host_items = list(self._iter_resources(RESOURCE_KIND_HOST))

        # 전체 VM이 계층 조회에 실패했을 때 "이름이 틀린 것"과 "관계 자체를 못 받아온 것"을
        # 구분해 알려주기 위해 모든 단계에 걸쳐 공유하는 진단 정보.
        diagnostics: dict = {"observed_kinds": set(), "any_children_found": False}

        def _record(children: list[dict]) -> None:
            if children:
                diagnostics["any_children_found"] = True
            for child in children:
                kind = child.get("resourceKey", {}).get("resourceKindKey")
                if kind:
                    diagnostics["observed_kinds"].add(kind)

        # 1) Datacenter -> Cluster
        cluster_datacenter: dict[str, dict] = {}
        for dc in dc_items:
            children = self._get_children(dc["identifier"], children_cache)
            _record(children)
            for child in children:
                kind = child.get("resourceKey", {}).get("resourceKindKey")
                canonical = _HIERARCHY_KIND_ALIASES.get(kind.strip().lower()) if kind else None
                if canonical == RESOURCE_KIND_CLUSTER:
                    cluster_datacenter[child["identifier"]] = dc

        # 2) Cluster -> HostSystem
        host_cluster: dict[str, dict] = {}
        for cl in cluster_items:
            children = self._get_children(cl["identifier"], children_cache)
            _record(children)
            for child in children:
                kind = child.get("resourceKey", {}).get("resourceKindKey")
                canonical = _HIERARCHY_KIND_ALIASES.get(kind.strip().lower()) if kind else None
                if canonical == RESOURCE_KIND_HOST:
                    host_cluster[child["identifier"]] = cl

        # 3) HostSystem -> VirtualMachine
        vm_host: dict[str, dict] = {}
        for h in host_items:
            children = self._get_children(h["identifier"], children_cache)
            _record(children)
            for child in children:
                kind = child.get("resourceKey", {}).get("resourceKindKey")
                canonical = _HIERARCHY_KIND_ALIASES.get(kind.strip().lower()) if kind else None
                if canonical == RESOURCE_KIND_VM:
                    vm_host[child["identifier"]] = h

        # 4) VM Folder (선택 사항 - 실패해도 계속 진행)
        try:
            vm_folder = self._resolve_folders(dc_items, children_cache)
        except httpx.HTTPError:
            logger.exception("VM Folder 계층 조회 중 통신 오류 - Folder 없이 계속합니다")
            vm_folder = {}

        snapshots: list[VMSnapshot] = []
        hierarchy_failures = 0
        excluded_own_count = 0

        for item in vm_items:
            vm_id = item["identifier"]
            vm_name = item.get("resourceKey", {}).get("name", vm_id)
            try:
                props = self._fetch_properties(vm_id)
            except httpx.HTTPError:
                logger.exception("VM %s(%s) 프로퍼티 조회 실패, 건너뜁니다", vm_name, vm_id)
                continue

            # [v4.2] 이 연동 계정 자신의 VCF Operations 어플라이언스는 과금 대상
            # 인벤토리에서 제외한다 ("인벤토리 정보에 operations 본인 도메인 자체는
            # 제외" 요청) - hierarchy_failures 판정에는 영향을 주지 않도록(진짜 계층
            # 조회 실패와 구분하기 위해) 카운트 없이 그냥 건너뛴다.
            if self._is_own_appliance_vm(vm_name, props):
                logger.info(
                    "VM %s(%s) 는 이 연동 계정 자신의 어플라이언스(%s)로 판단되어 인벤토리에서 제외합니다",
                    vm_name,
                    vm_id,
                    self._conn.base_url,
                )
                excluded_own_count += 1
                continue

            host = vm_host.get(vm_id)
            cluster = host_cluster.get(host["identifier"]) if host else None
            datacenter = cluster_datacenter.get(cluster["identifier"]) if cluster else None

            # [v3.5] relationships 체인이 이 VM에 대해서만 끊긴 경우(전체 실패는 위에서
            # 이미 별도로 처리됨), VM 자신의 "Parent Datacenter"/"Parent Cluster"
            # 프로퍼티로 보정을 시도한다. Folder와 달리 Datacenter/Cluster는 hierarchy_
            # failures 판정에 쓰이므로, 이 VM 하나를 통째로 건너뛰는 대신 살릴 수 있다면
            # 살린다. (참고: relationships로 정상 resolve된 다른 VM과 같은 실제 Datacenter/
            # Cluster를 프로퍼티 값으로도 가리키는 경우, 실제 리소스 id 대신 이름 기반의
            # 별도 식별자("by-name:...")로 저장되어 드물게 같은 대상이 두 행으로 중복될 수
            # 있다 - relationships 기반 조회가 일부 VM에서만 실패하는 흔치 않은 경우에 한한
            # 안전망이라 감수한다.)
            if datacenter is None:
                dc_found = _find_property_by_name_fragment(
                    props, _PARENT_DATACENTER_NAME_FRAGMENTS, _PARENT_HIERARCHY_EXCLUDE_FRAGMENTS
                )
                if dc_found:
                    dc_name = dc_found[1]
                    datacenter = {"identifier": f"by-name:{dc_name}", "resourceKey": {"name": dc_name}}
            if cluster is None:
                cl_found = _find_property_by_name_fragment(
                    props, _PARENT_CLUSTER_NAME_FRAGMENTS, _PARENT_HIERARCHY_EXCLUDE_FRAGMENTS
                )
                if cl_found:
                    cl_name = cl_found[1]
                    cluster = {"identifier": f"by-name:{cl_name}", "resourceKey": {"name": cl_name}}

            if datacenter is None:
                hierarchy_failures += 1
                logger.warning(
                    "VM %s(%s) 상위 Datacenter/Cluster/HostSystem 관계를 찾지 못했습니다, 건너뜁니다",
                    vm_name,
                    vm_id,
                )
                continue

            # [v3.5] Folder는 relationships(CHILD) 기반 조회를 먼저 쓰고, 그걸로 하나도
            # 못 찾았다면(이 환경처럼 Folder가 relationships에 전혀 노출되지 않는 경우)
            # VM 자신의 "Parent Folder"/"vSphere Folder" 프로퍼티로 보완한다.
            folder_info = vm_folder.get(vm_id) or self._extract_folder_from_properties(props)
            tags = self._extract_tags_from_properties(props)

            # [v3.7] 사용량 가중치 하이브리드 과금 입력값. 설정으로 끌 수 있다
            # (app/config.py의 collect_usage_metrics) - 대규모 환경에서 VM당 API 호출이
            # 1개 늘어나는 것을 원치 않고 가중치 과금도 안 쓸 계획이면 꺼두면 된다.
            if collect_usage_metrics:
                cpu_usage_pct, mem_usage_pct = self._fetch_usage_pct(vm_id)
            else:
                cpu_usage_pct, mem_usage_pct = None, None

            # [v3.6] vCenter는 기본적으로 계정의 base_url에서 유도한 값을 쓰지만
            # (한 연동 계정 = vCenter/스코프 하나라는 전제), 사용자가 실제 환경에서
            # 수집한 샘플을 보면 VM 자신의 "Parent vCenter" 프로퍼티로 실제 vCenter
            # 호스트명을 직접 노출하고 있었다 - 이 값이 있으면 그걸 우선 쓴다(Aria
            # Operations 한 인스턴스가 여러 vCenter를 관리하는 환경에서 더 정확함).
            # by-name 식별자를 쓰므로, 같은 이름이면 수집 주기마다 같은 VCenter 행으로
            # 계속 합쳐진다.
            vcenter_found = _find_property_by_name_fragment(
                props, _PARENT_VCENTER_NAME_FRAGMENTS, _PARENT_HIERARCHY_EXCLUDE_FRAGMENTS
            )
            if vcenter_found:
                vcenter_external_id = f"by-name:{vcenter_found[1]}"
                vcenter_name = vcenter_found[1]
            else:
                vcenter_external_id = self._fallback_vcenter_id
                vcenter_name = self._fallback_vcenter_name

            snapshots.append(
                VMSnapshot(
                    external_id=vm_id,
                    name=vm_name,
                    vcenter_external_id=vcenter_external_id,
                    vcenter_name=vcenter_name,
                    datacenter_external_id=datacenter["identifier"],
                    datacenter_name=datacenter["resourceKey"]["name"],
                    cluster_external_id=cluster["identifier"] if cluster else None,
                    cluster_name=cluster["resourceKey"]["name"] if cluster else None,
                    folder_external_id=folder_info["folder_external_id"] if folder_info else None,
                    folder_path=folder_info["folder_path"] if folder_info else None,
                    folder_name=folder_info["folder_name"] if folder_info else None,
                    vcpu_count=int(float(props.get(PROP_NUM_CPU, 0) or 0)),
                    vmem_gb=round(float(props.get(PROP_MEM_KB, 0) or 0) / (1024 * 1024), 2),
                    vdisk_gb=_extract_vdisk_gb(props),
                    os_name=str(props.get(PROP_GUEST_OS, "")),
                    power_state=_extract_power_state(props),
                    tags=tags,
                    cpu_usage_pct=cpu_usage_pct,
                    mem_usage_pct=mem_usage_pct,
                )
            )

        # VM은 있는데 단 한 대도 상위 계층을 못 찾았다면, 십중팔구 RESOURCE_KIND_*
        # 상수가 이 환경과 맞지 않거나(경우 A) relationships 응답 자체를 못 받아온
        # 것이다(경우 B) - vm_count=0인 "성공"으로 조용히 넘어가지 않고, 두 경우를
        # 구분해서 바로 원인을 알 수 있는 에러로 확실히 실패시킨다. [v4.2] 자기 자신의
        # 어플라이언스로 판단해 건너뛴 VM은 애초에 계층 판정 대상이 아니었으므로
        # 분모에서 제외한다 - 그렇지 않으면 어플라이언스 1대만 제외되고 나머지가 전부
        # 정상 처리된 경우에도 "전부 실패"로 잘못 판정될 수 있다.
        considered = len(vm_items) - excluded_own_count
        if considered > 0 and hierarchy_failures == considered:
            if not diagnostics["any_children_found"]:
                raise VCFOpsIntegrationError(
                    f"VM {considered}대 전부 Datacenter/Cluster/HostSystem 하위 관계(relationships) "
                    "자체를 하나도 받지 못했습니다. 이는 RESOURCE_KIND_* 이름이 아니라, relationships "
                    "API 응답 형식 자체가 예상과 다르다는 뜻일 가능성이 높습니다 - 서버 로그에 "
                    "'relationships 응답에서 알려진 키를 찾지 못했습니다'라는 경고가 함께 남았다면 "
                    "거기 적힌 실제 최상위 키 이름을 vcf_ops_client.py의 _RELATIONSHIP_LIST_KEYS에 "
                    "추가하세요. 로그에 그 경고가 없다면 relationshipType=CHILD 파라미터 자체가 이 "
                    "버전에서 다르게 동작하는 것일 수 있습니다 - "
                    "scripts/inspect_integration_account.py 로 원본 응답을 직접 확인하세요."
                )
            observed = ", ".join(sorted(diagnostics["observed_kinds"])) or "(발견된 종류 없음)"
            raise VCFOpsIntegrationError(
                f"VM {considered}대 전부 Datacenter->Cluster->HostSystem->VM 하위 관계 체인을 "
                f"끝까지 연결하지 못했습니다. 하위 관계에서 실제로 발견된 리소스 종류: {observed}. "
                f"vcf_ops_client.py 상단의 RESOURCE_KIND_CLUSTER(현재 '{RESOURCE_KIND_CLUSTER}')/"
                f"RESOURCE_KIND_HOST(현재 '{RESOURCE_KIND_HOST}')를 위 목록에 있는 실제 값으로 "
                "바꾸세요."
            )

        return snapshots

    def _fetch_properties(self, vm_id: str) -> dict:
        resp = self._get(f"/suite-api/api/resources/{vm_id}/properties")
        return {p["name"]: p.get("value") for p in resp.json().get("property", [])}

    # -- 사용량(가중치 하이브리드 과금 입력값) ---------------------------------
    def _fetch_usage_pct(self, vm_id: str) -> tuple[float | None, float | None]:
        """(cpu_usage_pct, mem_usage_pct)를 조회한다 (VM당 API 호출 1개 추가).

        [v3.7] 이 조회는 사용량 가중치 하이브리드 과금(app/billing/engine.py)에만
        쓰이는 부가 정보라, 실패해도 VM 수집 자체를 막지 않는다 - 이미 완성된 Cluster/
        Folder/Tag/스펙 수집 경로(v3.4~v3.6)를 이 기능 때문에 깨뜨리지 않기 위함이다.
        조회에 실패하거나 이 환경에 해당 stat key가 없으면 (None, None)을 반환하고,
        과금 엔진은 이를 "가중치 없음(기존과 동일하게 100% 과금)"으로 처리한다.
        """
        try:
            body = self._get(
                f"/suite-api/api/resources/{vm_id}/stats/latest",
                params=[("statKey", _STAT_KEY_CPU_USAGE), ("statKey", _STAT_KEY_MEM_USAGE)],
            ).json()
        except httpx.HTTPError:
            logger.debug("VM %s 사용량(stats/latest) 조회 실패 - 가중치 없이 계속합니다", vm_id, exc_info=True)
            return None, None

        cpu_usage_pct: float | None = None
        mem_usage_pct: float | None = None
        for value_block in body.get("values", []):
            for stat in (value_block.get("stat-list") or {}).get("stat", []):
                key = (stat.get("statKey") or {}).get("key")
                data = stat.get("data") or []
                if not data:
                    continue
                if key == _STAT_KEY_CPU_USAGE:
                    cpu_usage_pct = float(data[-1])
                elif key == _STAT_KEY_MEM_USAGE:
                    mem_usage_pct = float(data[-1])
        return cpu_usage_pct, mem_usage_pct

    # -- 태그 ---------------------------------------------------------------
    def _extract_tags_from_properties(self, properties: dict) -> list[TagRef]:
        """VM 프로퍼티에서 태그를 파싱한다. 세 가지 형태를 모두 지원한다.

        1. (v3.2 방식) TAG_PROPERTY_PREFIX("summary|tag|")로 시작하는 프로퍼티 - 태그
           하나당 별도 프로퍼티 "prefix|카테고리|이름"(또는 세그먼트 1개면
           TAG_DEFAULT_CATEGORY 사용). 값이 명시적으로 false/0이면 "해당 태그 없음"으로
           보고 제외한다.
        2. [v3.5] "vSphere Tag" 계열 프로퍼티(정규화된 키에 "spheretag"가 포함) - 키
           자체에 이미 카테고리/이름이 세그먼트로 들어있으면 그걸 쓰고
           (예: "...|vsphereTag|Env|Prod"), 그렇지 않고 이 키가 곧 태그의 끝(leaf)이면
           값 문자열을 ","/";" 로 구분된 다중 태그 목록으로 보고 각각 파싱한다.
        3. [v3.6] 사용자가 scripts/inspect_vm_metrics.py로 실제 환경에서 수집한 원본
           샘플을 보면, 이 환경은 위 두 형태 어디에도 안 걸리는 "summary|tag"(세그먼트
           없는 단일 프로퍼티)와 "summary|tagJson" 프로퍼티를 쓰고 있었다(둘 다
           정규화해도 "spheretag"를 포함하지 않는다). 태그가 없을 때는 값이 문자열
           리터럴 "none"이었다 - 이 값은 무조건 건너뛴다. "summary|tagJson"은 이름 그대로
           JSON 배열일 가능성이 높아 json.loads를 먼저 시도하고(각 원소가 문자열이면
           "카테고리:이름" 형식으로, dict면 category/name류의 흔한 키 이름 후보로 파싱),
           JSON이 아니면 2번과 동일한 구분자 파싱으로 대체한다. 오탐을 피하기 위해
           정규화된 키가 이 두 이름과 "정확히" 일치할 때만 이 규칙을 적용한다(포함
           여부가 아님) - "tag"라는 단어가 들어간 무관한 프로퍼티까지 집지 않기 위함.

        VM 프로퍼티는 스펙 조회를 위해 어차피 한 번 가져오므로, 태그만을 위한 별도
        API 호출 없이 재사용한다.
        """
        tags: list[TagRef] = []
        seen: set[tuple[str, str]] = set()

        def add(category: str | None, name: str | None) -> None:
            cat = (category or TAG_DEFAULT_CATEGORY).strip() or TAG_DEFAULT_CATEGORY
            nm = (name or "").strip()
            if not nm:
                return
            key = (cat, nm)
            if key in seen:
                return
            seen.add(key)
            tags.append(TagRef(category=cat, name=nm))

        def add_from_delimited_string(value: str) -> None:
            for piece in re.split(r"[;,]", value):
                piece = piece.strip()
                if not piece:
                    continue
                if ":" in piece:
                    cat, nm = piece.split(":", 1)
                elif "=" in piece:
                    cat, nm = piece.split("=", 1)
                else:
                    cat, nm = TAG_DEFAULT_CATEGORY, piece
                add(cat, nm)

        for key, value in properties.items():
            if not key.startswith(TAG_PROPERTY_PREFIX):
                continue
            if value is not None and str(value).strip().lower() in ("false", "0"):
                continue
            remainder = key[len(TAG_PROPERTY_PREFIX) :]
            segments = [s for s in remainder.split("|") if s]
            if not segments:
                continue
            if len(segments) >= 2:
                add(segments[0], segments[1])
            else:
                add(TAG_DEFAULT_CATEGORY, segments[0])

        for key, value in properties.items():
            if key.startswith(TAG_PROPERTY_PREFIX):
                continue  # 이미 위에서 처리됨
            norm = _normalize_property_name(key)
            if _VSPHERE_TAG_NAME_FRAGMENT not in norm:
                continue
            segments = [s for s in key.split("|") if s]
            tag_idx = next((i for i, s in enumerate(segments) if "tag" in s.lower()), None)
            extra = segments[tag_idx + 1 :] if tag_idx is not None else []
            if len(extra) >= 2:
                add(extra[0], extra[1])
            elif len(extra) == 1:
                add(TAG_DEFAULT_CATEGORY, extra[0])
            elif value is not None and str(value).strip():
                add_from_delimited_string(str(value))

        # [v3.6] "summary|tag"/"summary|tagJson" - 위 두 규칙 어디에도 안 걸리는,
        # 사용자 실제 환경에서 확인된 별도 태그 프로퍼티.
        for key, value in properties.items():
            if key.startswith(TAG_PROPERTY_PREFIX):
                continue
            norm = _normalize_property_name(key)
            if norm not in (_TAG_BARE_PROPERTY_NORMALIZED_KEY, _TAG_JSON_PROPERTY_NORMALIZED_KEY):
                continue
            if value is None:
                continue
            text = str(value).strip()
            if not text or text.lower() in _TAG_NONE_SENTINELS:
                continue

            if norm == _TAG_JSON_PROPERTY_NORMALIZED_KEY:
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, list):
                    for entry in parsed:
                        if isinstance(entry, dict):
                            cat = entry.get("category") or entry.get("categoryName") or entry.get("tagCategory")
                            nm = entry.get("name") or entry.get("tagName") or entry.get("value")
                            add(cat, nm)
                        elif isinstance(entry, str):
                            add_from_delimited_string(entry)
                    continue  # JSON 파싱에 성공했으면(빈 리스트여도) 문자열 폴백은 하지 않는다

            add_from_delimited_string(text)

        return tags
