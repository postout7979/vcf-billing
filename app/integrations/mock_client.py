"""
샘플(Mock) 데이터 소스.

is_mock=True 로 등록된 연동 계정(seed_data.py가 생성)은 실제 HTTP 호출 없이 이 모듈이
정의하는 가상의 vCenter/Datacenter/Cluster/VM Folder/Tag 계층과 VM 인벤토리, 그리고
"그럴듯한" 전원 On/Off 패턴을 사용한다 (결정론적 의사난수라 같은 VM+시각 조합이면 항상
같은 결과 -> 실시간 수집과 과거 백필(seed)이 서로 어긋나지 않음).

데모 인벤토리 하나(vcenter01.corp.local)에 Datacenter 2개(Nova-DC/Orion-DC)를 두고, 그
안에 각각 Cluster 2개 + VM Folder 2개 + VM들을 배치한다. seed_data.py는 이 하나의
연동 계정 인벤토리에서 Tenant 2개(nova/orion)를 나눠 만들어, "연동은 독립적으로 한 번만
하고 그 인벤토리에서 여러 테넌트를 나눠 만든다"는 흐름을 그대로 보여준다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

from app.integrations.base import TagRef, VCFOpsClient, VMSnapshot

KST = dt.timezone(dt.timedelta(hours=9))

MOCK_VCENTER_EXTERNAL_ID = "vcenter-01"
MOCK_VCENTER_NAME = "vcenter01.corp.local"


@dataclass(frozen=True)
class MockVMDef:
    external_id: str
    name: str
    vcpu_count: int
    vmem_gb: float
    vdisk_gb: float
    os_name: str
    profile: str  # 전원 패턴 프로필: always_on / business_hours / bursty_dev / mostly_off
    datacenter_external_id: str
    datacenter_name: str
    cluster_external_id: str
    cluster_name: str
    folder_external_id: str
    folder_path: str
    folder_name: str
    tags: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (category, name) 쌍


# ---------------------------------------------------------------------------
# 데모 인벤토리 정의 (vCenter 1개 - Datacenter 2개 - Cluster/Folder 각 2개 - VM 16대)
# ---------------------------------------------------------------------------
MOCK_VM_DEFS: list[MockVMDef] = [
    # --- Nova-DC / Cluster-Prod-A / Prod 폴더 (운영, 상시 가동) ---
    MockVMDef("vm-nova-web-01", "alpha-web-01", 4, 8, 100, "Ubuntu Linux 22.04", "always_on",
               "dc-nova", "Nova-DC", "cluster-prod-a", "Cluster-Prod-A",
               "folder-nova-prod", "/Nova-DC/vm/Prod", "Prod",
               (("Environment", "Production"), ("CostCenter", "CC-100"))),
    MockVMDef("vm-nova-web-02", "alpha-web-02", 4, 8, 100, "Ubuntu Linux 22.04", "always_on",
               "dc-nova", "Nova-DC", "cluster-prod-a", "Cluster-Prod-A",
               "folder-nova-prod", "/Nova-DC/vm/Prod", "Prod",
               (("Environment", "Production"), ("CostCenter", "CC-100"))),
    MockVMDef("vm-nova-db-01", "alpha-db-01", 8, 32, 500, "Red Hat Enterprise Linux 9", "always_on",
               "dc-nova", "Nova-DC", "cluster-prod-a", "Cluster-Prod-A",
               "folder-nova-prod", "/Nova-DC/vm/Prod", "Prod",
               (("Environment", "Production"), ("CostCenter", "CC-100"))),
    MockVMDef("vm-nova-cache-01", "alpha-cache-01", 2, 8, 40, "Ubuntu Linux 22.04", "always_on",
               "dc-nova", "Nova-DC", "cluster-prod-a", "Cluster-Prod-A",
               "folder-nova-prod", "/Nova-DC/vm/Prod", "Prod",
               (("Environment", "Production"), ("CostCenter", "CC-100"))),
    # --- Nova-DC / Cluster-Dev-B / Dev 폴더 (개발/배치) ---
    MockVMDef("vm-nova-etl-01", "beta-etl-01", 8, 16, 200, "Ubuntu Linux 22.04", "bursty_dev",
               "dc-nova", "Nova-DC", "cluster-dev-b", "Cluster-Dev-B",
               "folder-nova-dev", "/Nova-DC/vm/Dev", "Dev",
               (("Environment", "Development"),)),
    MockVMDef("vm-nova-etl-02", "beta-etl-02", 8, 16, 200, "Ubuntu Linux 22.04", "bursty_dev",
               "dc-nova", "Nova-DC", "cluster-dev-b", "Cluster-Dev-B",
               "folder-nova-dev", "/Nova-DC/vm/Dev", "Dev",
               (("Environment", "Development"),)),
    MockVMDef("vm-nova-notebook-01", "beta-notebook-01", 4, 16, 80, "Ubuntu Linux 22.04", "business_hours",
               "dc-nova", "Nova-DC", "cluster-dev-b", "Cluster-Dev-B",
               "folder-nova-dev", "/Nova-DC/vm/Dev", "Dev",
               (("Environment", "Development"),)),
    MockVMDef("vm-nova-spark-01", "beta-spark-driver-01", 4, 16, 100, "Ubuntu Linux 22.04", "bursty_dev",
               "dc-nova", "Nova-DC", "cluster-dev-b", "Cluster-Dev-B",
               "folder-nova-dev", "/Nova-DC/vm/Dev", "Dev",
               (("Environment", "Development"),)),
    # --- Orion-DC / Cluster-QA / QA 폴더 ---
    MockVMDef("vm-orion-web-01", "gamma-web-01", 2, 4, 60, "Ubuntu Linux 22.04", "business_hours",
               "dc-orion", "Orion-DC", "cluster-qa", "Cluster-QA",
               "folder-orion-qa", "/Orion-DC/vm/QA", "QA",
               (("Environment", "QA"), ("CostCenter", "CC-200"))),
    MockVMDef("vm-orion-app-01", "gamma-app-01", 4, 8, 80, "Windows Server 2022", "business_hours",
               "dc-orion", "Orion-DC", "cluster-qa", "Cluster-QA",
               "folder-orion-qa", "/Orion-DC/vm/QA", "QA",
               (("Environment", "QA"), ("CostCenter", "CC-200"))),
    MockVMDef("vm-orion-db-01", "gamma-db-01", 4, 16, 200, "Red Hat Enterprise Linux 9", "business_hours",
               "dc-orion", "Orion-DC", "cluster-qa", "Cluster-QA",
               "folder-orion-qa", "/Orion-DC/vm/QA", "QA",
               (("Environment", "QA"), ("CostCenter", "CC-200"))),
    # --- Orion-DC / Cluster-Sandbox / Sandbox 폴더 ---
    MockVMDef("vm-orion-dev-01", "sandbox-dev-01", 2, 4, 40, "Ubuntu Linux 22.04", "mostly_off",
               "dc-orion", "Orion-DC", "cluster-sandbox", "Cluster-Sandbox",
               "folder-orion-sandbox", "/Orion-DC/vm/Sandbox", "Sandbox",
               (("Environment", "Sandbox"),)),
    MockVMDef("vm-orion-dev-02", "sandbox-dev-02", 2, 4, 40, "Ubuntu Linux 22.04", "mostly_off",
               "dc-orion", "Orion-DC", "cluster-sandbox", "Cluster-Sandbox",
               "folder-orion-sandbox", "/Orion-DC/vm/Sandbox", "Sandbox",
               (("Environment", "Sandbox"),)),
    MockVMDef("vm-orion-dev-03", "sandbox-dev-03", 4, 8, 60, "Windows 11", "mostly_off",
               "dc-orion", "Orion-DC", "cluster-sandbox", "Cluster-Sandbox",
               "folder-orion-sandbox", "/Orion-DC/vm/Sandbox", "Sandbox",
               (("Environment", "Sandbox"),)),
    MockVMDef("vm-orion-test-01", "sandbox-test-01", 2, 4, 40, "Ubuntu Linux 22.04", "mostly_off",
               "dc-orion", "Orion-DC", "cluster-sandbox", "Cluster-Sandbox",
               "folder-orion-sandbox", "/Orion-DC/vm/Sandbox", "Sandbox",
               (("Environment", "Sandbox"),)),
    MockVMDef("vm-orion-test-02", "sandbox-test-02", 1, 2, 20, "Ubuntu Linux 22.04", "mostly_off",
               "dc-orion", "Orion-DC", "cluster-sandbox", "Cluster-Sandbox",
               "folder-orion-sandbox", "/Orion-DC/vm/Sandbox", "Sandbox",
               (("Environment", "Sandbox"),)),
]


def _rand(*parts: object) -> float:
    """결정론적 의사난수 (0.0 ~ 1.0)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16) / 0xFFFFFFFFFFFF


# [v3.7] 프로필별 실사용률(%) 범위 - (cpu_base, cpu_span, mem_base, mem_span).
# always_on(상시 운영)이 가장 사용률이 높고 안정적이고, mostly_off(간헐 사용)가 가장
# 낮다 - 사용량 가중치 하이브리드 과금을 데모에서 프로필별로 눈에 띄게 다르게 보여주기
# 위한 설계로, 실제 vROps 사용률 분포를 흉내낸 것일 뿐 특정 조사에 근거한 값은 아니다.
_USAGE_PROFILE_RANGES: dict[str, tuple[float, float, float, float]] = {
    "always_on": (35.0, 30.0, 55.0, 25.0),
    "business_hours": (20.0, 30.0, 40.0, 25.0),
    "bursty_dev": (10.0, 75.0, 30.0, 40.0),
    "mostly_off": (5.0, 20.0, 20.0, 25.0),
}


def compute_usage_pct(profile: str, external_id: str, at_utc: dt.datetime, power_state: str) -> tuple[float | None, float | None]:
    """결정론적 의사난수로 (cpu_usage_pct, mem_usage_pct)를 계산한다. 꺼져 있으면 (None, None)."""
    if power_state != "on":
        return None, None
    cpu_base, cpu_span, mem_base, mem_span = _USAGE_PROFILE_RANGES.get(profile, (10.0, 20.0, 20.0, 20.0))
    cpu = cpu_base + cpu_span * _rand(external_id, "cpu-usage", at_utc.isoformat())
    mem = mem_base + mem_span * _rand(external_id, "mem-usage", at_utc.isoformat())
    return round(min(cpu, 100.0), 1), round(min(mem, 100.0), 1)


def compute_power_state(profile: str, external_id: str, at_utc: dt.datetime) -> str:
    """주어진 시각(UTC)에 해당 프로필의 VM이 켜져 있을지를 결정론적으로 계산한다."""
    local = at_utc.astimezone(KST)
    weekday = local.weekday()  # 0=Mon .. 6=Sun
    hour = local.hour
    day_key = local.strftime("%Y-%m-%d")

    if profile == "always_on":
        # 아주 가끔(0.3%) 짧은 점검창으로 꺼짐
        return "off" if _rand(external_id, "maint", at_utc.isoformat()) < 0.003 else "on"

    if profile == "business_hours":
        if weekday >= 5:  # 주말: 드물게 온콜 점검
            return "on" if _rand(external_id, "weekend", at_utc.isoformat()) < 0.04 else "off"
        if 8 <= hour < 20:
            return "off" if _rand(external_id, "gap", at_utc.isoformat()) < 0.02 else "on"
        return "on" if _rand(external_id, "late", at_utc.isoformat()) < 0.05 else "off"

    if profile == "bursty_dev":
        # 하루 단위로 "활성일"인지 결정 (~45%), 활성일에는 8~24시 사이 70% 가동
        if _rand(external_id, "day", day_key) > 0.45:
            return "off"
        if 7 <= hour < 24:
            return "on" if _rand(external_id, "slot", at_utc.isoformat()) < 0.7 else "off"
        return "on" if _rand(external_id, "night", at_utc.isoformat()) < 0.15 else "off"

    if profile == "mostly_off":
        # 하루 단위로 "사용일"인지 결정 (~18%), 사용일에는 낮 시간대 절반 정도 가동
        if _rand(external_id, "day", day_key) > 0.18:
            return "off"
        if 9 <= hour < 22:
            return "on" if _rand(external_id, "slot", at_utc.isoformat()) < 0.5 else "off"
        return "off"

    return "off"


class MockVCFOpsClient(VCFOpsClient):
    """샘플 데이터 기반 VCF Operations 클라이언트 구현체."""

    def __init__(self, vm_defs: list[MockVMDef] | None = None):
        self._vm_defs = vm_defs if vm_defs is not None else MOCK_VM_DEFS

    def list_vm_snapshot(self) -> list[VMSnapshot]:
        now = _floor_to_interval(dt.datetime.now(dt.timezone.utc), 5)
        snapshots = []
        for v in self._vm_defs:
            power_state = compute_power_state(v.profile, v.external_id, now)
            cpu_usage_pct, mem_usage_pct = compute_usage_pct(v.profile, v.external_id, now, power_state)
            snapshots.append(
                VMSnapshot(
                    external_id=v.external_id,
                    name=v.name,
                    vcenter_external_id=MOCK_VCENTER_EXTERNAL_ID,
                    vcenter_name=MOCK_VCENTER_NAME,
                    datacenter_external_id=v.datacenter_external_id,
                    datacenter_name=v.datacenter_name,
                    cluster_external_id=v.cluster_external_id,
                    cluster_name=v.cluster_name,
                    folder_external_id=v.folder_external_id,
                    folder_path=v.folder_path,
                    folder_name=v.folder_name,
                    vcpu_count=v.vcpu_count,
                    vmem_gb=v.vmem_gb,
                    vdisk_gb=v.vdisk_gb,
                    os_name=v.os_name,
                    power_state=power_state,
                    tags=[TagRef(category=c, name=n) for c, n in v.tags],
                    cpu_usage_pct=cpu_usage_pct,
                    mem_usage_pct=mem_usage_pct,
                )
            )
        return snapshots


def build_mock_client() -> MockVCFOpsClient:
    """데모 연동 계정(is_mock=True)용 샘플 데이터 클라이언트를 생성한다."""
    return MockVCFOpsClient(MOCK_VM_DEFS)


def _floor_to_interval(t: dt.datetime, minutes: int) -> dt.datetime:
    return t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)
