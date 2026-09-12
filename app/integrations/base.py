"""
VCF Operations / Aria Operations 연동 공통 인터페이스.

billing collector / seed 스크립트는 이 인터페이스에만 의존하고, 실제 데이터 소스가
샘플(Mock)인지 실 VCF Operations/Aria Operations REST API인지는 알지 못한다. 덕분에
운영 전환 시에는 상위 로직(app/collector.py)을 건드리지 않고, "계정 연동" 관리자 화면에서
연동 계정을 등록/수정/삭제하는 것만으로 데이터 소스를 바꿀 수 있다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class TagRef:
    """VM에 부여된 태그 (category+name)."""

    category: str
    name: str


@dataclass
class VMSnapshot:
    """특정 시점의 VM 상태 스냅샷.

    vCenter -> Datacenter -> Cluster/VM Folder 계층 정보와 태그를 모두 담아, 상위
    (collector)가 상호 연결성을 인식할 수 있는 정규화 테이블(VCenter/Datacenter/Cluster/
    VMFolder/Tag)을 구성하고 Project의 다중 선택 매칭 기준(Cluster/Folder/Tag)에
    사용할 수 있게 한다. cluster/folder는 VM에 따라 없을 수 있어 None을 허용한다
    (예: 클러스터 밖의 standalone host, 커스텀 폴더 없이 최상위에 있는 VM).
    """

    external_id: str          # VCF Operations / vCenter 상의 고유 식별자 (moref 등)
    name: str

    vcenter_external_id: str
    vcenter_name: str
    datacenter_external_id: str
    datacenter_name: str

    cluster_external_id: str | None
    cluster_name: str | None
    folder_external_id: str | None
    folder_path: str | None
    folder_name: str | None

    vcpu_count: int
    vmem_gb: float
    vdisk_gb: float
    os_name: str
    power_state: str          # "on" | "off"

    tags: list[TagRef] = field(default_factory=list)

    # [v3.7] 실사용률(%, 0~100) - 사용량 가중치 하이브리드 과금(app/billing/engine.py)의
    # 입력값. 구하지 못하면(연동 미지원, 조회 실패 등) None - collector가 그대로
    # PowerSample.cpu_usage_pct/mem_usage_pct에 흘려보내고, 과금 엔진은 None을 "가중치
    # 없음(기존과 동일하게 100% 과금)"으로 처리하므로 이 필드가 없어도 기존 동작이
    # 깨지지 않는다.
    cpu_usage_pct: float | None = None
    mem_usage_pct: float | None = None


class VCFOpsClient(ABC):
    """VCF Operations/Aria Operations 연동 어댑터가 구현해야 하는 최소 인터페이스."""

    @abstractmethod
    def list_vm_snapshot(self) -> list[VMSnapshot]:
        """
        현재 시점 VM 인벤토리 스냅샷을 반환한다 (vCenter/Datacenter/Cluster/Folder/Tag
        관계 및 vCPU/vMEM/vDisk 스펙 포함).

        collector 가 COLLECTOR_INTERVAL_MINUTES(기본 5분) 주기로, 등록된 연동 계정마다
        호출하여 인벤토리 계층을 업서트하고 PowerSample 로 적재한다. 이 호출 하나가 곧
        하나의 과금 단위(수집 간격 블록)가 된다.
        """
        raise NotImplementedError

    def close(self) -> None:
        """이 클라이언트가 보유한 연결(HTTP 커넥션 풀 등)을 정리한다.

        build_client_for_integration_account() 는 수집 사이클마다 새 클라이언트를
        만들기 때문에, 실 연동 구현체(VCFOpsRestClient)는 반드시 이를 오버라이드해서
        내부 HTTP 클라이언트를 닫아야 한다 (그러지 않으면 커넥션이 누적된다).
        Mock 구현체 등 정리할 리소스가 없는 경우에는 기본 no-op 이면 충분하다.
        """
        return None
