"""
Operations DB 데이터 모델 (v4.9: Billing DB / Operations DB 논리 분리).

[v4.9] 이 파일은 원래 app/models.py에 있던 VM 인벤토리 수집 데이터 모델을, Billing DB
(app/models.py)와 물리적으로 분리될 수 있는 별도 데이터베이스로 옮긴 것이다
(app/database.py의 OpsBase/ops_engine/OpsSessionLocal 참고). "Operations의 데이터
수집"과 "빌링 시스템 구동을 위한 기본 데이터베이스"를 분리한다는 설계 결정에 따른 것으로,
기본값(OPERATIONS_DATABASE_URL 미설정)에서는 Billing DB와 같은 물리 DB에 테이블만
나뉘어 생성되지만, 관리자가 별도 DB를 설정하면 코드 변경 없이 완전히 분리된다.

- IntegrationAccount : VCF Operations/Aria Operations 연동 계정. 특정 Tenant에 종속되지
                       않는 독립적인 전역 엔티티다 ("계정 연동" 메뉴에서 별도로 관리). 하나의
                       연동 계정에서 수집한 인벤토리(vCenter~VM)를 여러 Tenant/Project가
                       나눠서 사용할 수 있다.
- VCenter/Datacenter/Cluster/VMFolder
                     : 연동 계정에서 수집한 인벤토리 계층 구조 (vCenter -> Datacenter ->
                       Cluster/VMFolder). 상호 연결 관계를 그대로 정규화해서 저장한다.
- Tag               : VM에 부여된 태그 (category+name). 연동 계정 범위에서 유일하다.
- VirtualMachine    : 연동 계정에서 수집한 VM 인벤토리. 소속 vCenter/Datacenter/Cluster/
                       Folder 및 태그를 모두 보관한다. project_id는 Billing DB(Project)의
                       매칭 기준에 따라 수집기가 매 주기 재계산하는 값이지만, Project가
                       다른 물리 DB에 있을 수 있어 ForeignKey는 걸지 않는다(소프트 참조 -
                       app/models.py 상단 주석 참고).
- PowerSample       : 수집 주기(기본 5분)마다 적재하는 VM 전원상태 + 스펙 스냅샷 (과금 원천).

Project<->Cluster/VMFolder/Tag의 다중 선택 매칭 기준 연결 테이블(project_cluster_link 등)은
project_id가 실제로 가리키는 Billing DB 쪽(Project.id, 같은 물리 DB)에 두는 것이 자연스러워
app/models.py에 그대로 남아 있다 - 이 파일에는 없다. VM의 태그 연결(vm_tag_link)은 VM/Tag
모두 이 Operations DB 안에 있으므로 평범한 FK가 있는 정상적인 다대다 관계다.
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import OpsBase


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PowerState(str, enum.Enum):
    ON = "on"
    OFF = "off"


class IntegrationKind(str, enum.Enum):
    """연동 제품 종류.

    [v4.2] Aria Operations는 선택지에서 제거했다(관리자 화면 요청) - VCF Operations
    하나만 지원한다. 기존에 kind="aria_ops"로 저장된 행이 있는 상태에서 업그레이드하면
    SQLAlchemy Enum이 그 값을 더 이상 유효한 멤버로 인식하지 못해 해당 계정을 읽을 때
    오류가 난다 - 업그레이드 전에 그런 계정이 있다면 DB에서 직접
    `UPDATE integration_accounts SET kind='vcf_ops' WHERE kind='aria_ops'`로 정리하세요
    (API 형태 자체는 두 제품이 동일해 실제 동작에는 차이가 없다).
    """

    VCF_OPS = "vcf_ops"


# VM의 다중 태그 (VM/Tag 모두 Operations DB 안에 있으므로 정상적인 FK 다대다 연결 테이블)
vm_tag_link = Table(
    "vm_tag_link",
    OpsBase.metadata,
    Column("vm_id", ForeignKey("virtual_machines.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class IntegrationAccount(OpsBase):
    """
    VCF Operations/Aria Operations 연동 계정 (독립/전역).

    password_encrypted 는 평문이 아니라 app/security/crypto.py 의 encrypt_secret() 으로
    암호화된 값이며, 실제 연동 호출 직전에만 decrypt_secret() 으로 복호화해서 사용한다.
    API 응답에는 절대 포함하지 않는다 (schemas.IntegrationAccountOut 참고).

    is_mock 은 데모/시딩 전용 내부 플래그로, 관리자 API로는 설정할 수 없다(seed_data.py가
    직접 ORM으로 생성). True면 실제 HTTP 호출 없이 샘플 인벤토리를 생성한다.
    """

    __tablename__ = "integration_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)

    kind: Mapped[IntegrationKind] = mapped_column(Enum(IntegrationKind), default=IntegrationKind.VCF_OPS)
    name: Mapped[str] = mapped_column(String(128), default="")
    base_url: Mapped[str] = mapped_column(String(256))
    username: Mapped[str] = mapped_column(String(256))
    password_encrypted: Mapped[str] = mapped_column(String(1024))
    auth_source: Mapped[str] = mapped_column(String(64), default="local")
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)

    # 연동 상태 - "가져오기"(수동 즉시 수집) 및 5분 주기 자동 수집이 끝날 때마다 갱신된다.
    # last_sync_status: "never"(한 번도 시도 안 함) | "success" | "error"
    last_sync_status: Mapped[str] = mapped_column(String(32), default="never")
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_sync_error: Mapped[str | None] = mapped_column(String(2048), nullable=True, default=None)
    last_sync_vm_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(256), default="system")

    # [v4.10] 백업 파일에서 이 계정이 "복구로 되살아난" 것인지 표시하는 플래그.
    # app/backup_restore.py 참고 - 백업에는 연동 계정의 접속정보(base_url/username/
    # password_encrypted)를 포함하지 않으므로, 복구 시 이 값들을 빈 값으로 채우고 이
    # 플래그를 True로 세운다. 관리자 화면은 이 플래그가 True인 계정에 "재등록 필요"
    # 배지를 보여주고, 계정 수정 폼으로 실제 접속정보를 입력해 저장하면(기존 "계정
    # 수정" 흐름 그대로) id가 그대로 유지된 채 정상 계정으로 전환된다 - 이미 복구되어
    # 있던 VirtualMachine/PowerSample 등도 같은 id를 그대로 참조하고 있으므로 별도
    # 재연결 로직 없이 다음 수집 주기부터 기존 행이 그대로 갱신(upsert)된다.
    needs_reconnect: Mapped[bool] = mapped_column(Boolean, default=False)

    vcenters: Mapped[list["VCenter"]] = relationship(back_populates="integration_account", cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(back_populates="integration_account", cascade="all, delete-orphan")
    vms: Mapped[list["VirtualMachine"]] = relationship(back_populates="integration_account", cascade="all, delete-orphan")


class VCenter(OpsBase):
    """연동 계정에서 발견한 vCenter 인스턴스."""

    __tablename__ = "vcenters"
    __table_args__ = (UniqueConstraint("integration_account_id", "external_id", name="uq_vcenter_account_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_account_id: Mapped[int] = mapped_column(ForeignKey("integration_accounts.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    integration_account: Mapped["IntegrationAccount"] = relationship(back_populates="vcenters")
    datacenters: Mapped[list["Datacenter"]] = relationship(back_populates="vcenter", cascade="all, delete-orphan")


class Datacenter(OpsBase):
    """vCenter 하위 Datacenter."""

    __tablename__ = "datacenters"
    __table_args__ = (UniqueConstraint("vcenter_id", "external_id", name="uq_datacenter_vcenter_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vcenter_id: Mapped[int] = mapped_column(ForeignKey("vcenters.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    vcenter: Mapped["VCenter"] = relationship(back_populates="datacenters")
    clusters: Mapped[list["Cluster"]] = relationship(back_populates="datacenter", cascade="all, delete-orphan")
    folders: Mapped[list["VMFolder"]] = relationship(back_populates="datacenter", cascade="all, delete-orphan")


class Cluster(OpsBase):
    """Datacenter 하위 vSphere Cluster. Project의 다중 선택 매칭 기준 중 하나(id로 소프트 참조됨)."""

    __tablename__ = "clusters"
    __table_args__ = (UniqueConstraint("datacenter_id", "external_id", name="uq_cluster_dc_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    datacenter_id: Mapped[int] = mapped_column(ForeignKey("datacenters.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    datacenter: Mapped["Datacenter"] = relationship(back_populates="clusters")
    vms: Mapped[list["VirtualMachine"]] = relationship(back_populates="cluster")
    # [v4.9] Project는 Billing DB에 있어 relationship을 걸 수 없다 - 소속 Project를
    # 알아야 하면 app/project_criteria.py를 거쳐 id로 조회한다.


class VMFolder(OpsBase):
    """Datacenter 하위 VM Folder. Project의 다중 선택 매칭 기준 중 하나(id로 소프트 참조됨)."""

    __tablename__ = "vm_folders"
    __table_args__ = (UniqueConstraint("datacenter_id", "external_id", name="uq_folder_dc_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    datacenter_id: Mapped[int] = mapped_column(ForeignKey("datacenters.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    path: Mapped[str] = mapped_column(String(512))  # 예: "/Nova-DC/vm/Prod"
    name: Mapped[str] = mapped_column(String(256))  # 경로의 마지막 구성요소
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    datacenter: Mapped["Datacenter"] = relationship(back_populates="folders")
    vms: Mapped[list["VirtualMachine"]] = relationship(back_populates="folder")


class Tag(OpsBase):
    """VM에 부여된 태그 (category+name). Project의 다중 선택 매칭 기준 중 하나(id로 소프트 참조됨)."""

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("integration_account_id", "category", "name", name="uq_tag_account_cat_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_account_id: Mapped[int] = mapped_column(ForeignKey("integration_accounts.id"), index=True)
    category: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    integration_account: Mapped["IntegrationAccount"] = relationship(back_populates="tags")
    vms: Mapped[list["VirtualMachine"]] = relationship(secondary=vm_tag_link, back_populates="tags")

    @property
    def label(self) -> str:
        return f"{self.category}:{self.name}"


class VirtualMachine(OpsBase):
    __tablename__ = "virtual_machines"
    __table_args__ = (UniqueConstraint("integration_account_id", "external_id", name="uq_vm_account_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_account_id: Mapped[int] = mapped_column(ForeignKey("integration_accounts.id"), index=True)
    # VCF Operations / vCenter 상의 고유 식별자 (moref). 연동 계정 범위 내에서만 유일하면 된다.
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))

    vcenter_id: Mapped[int | None] = mapped_column(ForeignKey("vcenters.id"), nullable=True, index=True)
    datacenter_id: Mapped[int | None] = mapped_column(ForeignKey("datacenters.id"), nullable=True, index=True)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"), nullable=True, index=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("vm_folders.id"), nullable=True, index=True)

    # [v4.9] Project의 Cluster/Folder/Tag 다중 선택 기준에 따라 수집기가 매 주기
    # 재계산하는 값. Project는 Billing DB(다른 물리 DB일 수 있음)에 있으므로 이 컬럼은
    # 평범한 Integer일 뿐 ForeignKey가 아니다 - app/models.py 상단 주석 및
    # app/ops_queries.py 참고. 프로젝트 삭제 시에는 라우터가 이 값을 직접 None으로
    # 되돌린다(app/routers/admin.py).
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # 현재 스펙 (VM 인벤토리 상 최신 값)
    vcpu_count: Mapped[int] = mapped_column(Integer, default=0)
    vmem_gb: Mapped[float] = mapped_column(Float, default=0.0)
    vdisk_gb: Mapped[float] = mapped_column(Float, default=0.0)
    os_name: Mapped[str] = mapped_column(String(128), default="")
    current_power_state: Mapped[PowerState] = mapped_column(Enum(PowerState), default=PowerState.OFF)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    integration_account: Mapped["IntegrationAccount"] = relationship(back_populates="vms")
    vcenter: Mapped["VCenter | None"] = relationship()
    datacenter: Mapped["Datacenter | None"] = relationship()
    cluster: Mapped["Cluster | None"] = relationship(back_populates="vms")
    folder: Mapped["VMFolder | None"] = relationship(back_populates="vms")
    tags: Mapped[list["Tag"]] = relationship(secondary=vm_tag_link, back_populates="vms")
    samples: Mapped[list["PowerSample"]] = relationship(back_populates="vm", cascade="all, delete-orphan")


class PowerSample(OpsBase):
    """
    수집 주기(기본 5분)마다 적재하는 샘플. 과금 계산의 원천 데이터.

    한 행 = 해당 수집 블록 동안 VM이 관측된 전원상태 및 스펙.
    power_state == ON 인 행 개수 * 수집 간격(분) 이 곧 과금 대상 Power-On 시간이 된다.
    """

    __tablename__ = "power_samples"
    __table_args__ = (UniqueConstraint("vm_id", "sampled_at", name="uq_vm_sample_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vm_id: Mapped[int] = mapped_column(ForeignKey("virtual_machines.id"), index=True)
    sampled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    power_state: Mapped[PowerState] = mapped_column(Enum(PowerState))

    # 샘플 시점의 스펙 스냅샷 (향후 VM 스펙 변경 이력을 정확히 반영하기 위함)
    vcpu_count: Mapped[int] = mapped_column(Integer, default=0)
    vmem_gb: Mapped[float] = mapped_column(Float, default=0.0)
    vdisk_gb: Mapped[float] = mapped_column(Float, default=0.0)

    # [v3.7] 이 블록 시점의 실사용률(%, 0~100) - VCF Operations의 cpu|usage_average/
    # mem|usage_average에 해당. 수집 실패/미지원 환경/Mock에서 아직 계산 안 된 경우 등
    # 값을 못 구하면 None으로 남기며, 과금 엔진은 None을 "가중치 없음(=기존과 동일하게
    # 100% 과금)"으로 처리한다(app/billing/engine.py 참고) - 이 컬럼이 비어 있다고
    # 과금이 실패하거나 0원이 되지 않는다.
    cpu_usage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_usage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    vm: Mapped["VirtualMachine"] = relationship(back_populates="samples")
