"""
데이터 모델 (v3: 독립 연동계정 + 인벤토리 계층 + 다중 기준 프로젝트).

- IntegrationAccount : VCF Operations/Aria Operations 연동 계정. 더 이상 Tenant에 종속되지
                       않는 독립적인 전역 엔티티다 ("계정 연동" 메뉴에서 별도로 관리). 하나의
                       연동 계정에서 수집한 인벤토리(vCenter~VM)를 여러 Tenant/Project가
                       나눠서 사용할 수 있다.
- VCenter/Datacenter/Cluster/VMFolder
                     : 연동 계정에서 수집한 인벤토리 계층 구조 (vCenter -> Datacenter ->
                       Cluster/VMFolder). 상호 연결 관계를 그대로 정규화해서 저장한다.
- Tag               : VM에 부여된 태그 (category+name). 연동 계정 범위에서 유일하다.
- VirtualMachine    : 연동 계정에서 수집한 VM 인벤토리. 소속 vCenter/Datacenter/Cluster/
                       Folder 및 태그를 모두 보관한다. project_id는 Project의 매칭 기준에
                       따라 수집기가 매 주기 재계산하는 값이다.
- Project           : 과금 단위. Cluster/VM Folder/VM Tag 중 여러 종류를 동시에, 각각 여러
                       값을 다중 선택해서 매칭 기준으로 삼는다(OR 매칭). Tenant 하위에 속한다.
- Tenant            : 최상위 조직 단위. 하위 Project와 User를 소유한다. 특정 연동 계정에
                       종속되지 않으며, Project가 어떤 연동 계정의 인벤토리를 참조하든 상관없다.
- RateCard/History  : 프로젝트별 단가 및 변경 이력.
- PowerSample       : 수집 주기(기본 5분)마다 적재하는 VM 전원상태 + 스펙 스냅샷 (과금 원천).
- User              : 사용자 계정. role=admin 은 전체 Tenant 교차 조회, role=user 는 배정된
                       tenant_id 하나만 조회 가능. bcrypt 해시 비밀번호 사용.
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

from app.database import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PowerState(str, enum.Enum):
    ON = "on"
    OFF = "off"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class IntegrationKind(str, enum.Enum):
    """연동 제품 종류 (API 형태는 사실상 동일, 표시용 구분)."""

    VCF_OPS = "vcf_ops"
    ARIA_OPS = "aria_ops"


# ---------------------------------------------------------------------------
# 다대다 연결 테이블 (Project의 다중 선택 매칭 기준, VM의 다중 태그)
# ---------------------------------------------------------------------------

project_cluster_link = Table(
    "project_cluster_link",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("cluster_id", ForeignKey("clusters.id", ondelete="CASCADE"), primary_key=True),
)

project_folder_link = Table(
    "project_folder_link",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("folder_id", ForeignKey("vm_folders.id", ondelete="CASCADE"), primary_key=True),
)

project_tag_link = Table(
    "project_tag_link",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

vm_tag_link = Table(
    "vm_tag_link",
    Base.metadata,
    Column("vm_id", ForeignKey("virtual_machines.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class IntegrationAccount(Base):
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

    vcenters: Mapped[list["VCenter"]] = relationship(back_populates="integration_account", cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(back_populates="integration_account", cascade="all, delete-orphan")
    vms: Mapped[list["VirtualMachine"]] = relationship(back_populates="integration_account", cascade="all, delete-orphan")


class VCenter(Base):
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


class Datacenter(Base):
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


class Cluster(Base):
    """Datacenter 하위 vSphere Cluster. Project의 다중 선택 매칭 기준 중 하나."""

    __tablename__ = "clusters"
    __table_args__ = (UniqueConstraint("datacenter_id", "external_id", name="uq_cluster_dc_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    datacenter_id: Mapped[int] = mapped_column(ForeignKey("datacenters.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    datacenter: Mapped["Datacenter"] = relationship(back_populates="clusters")
    vms: Mapped[list["VirtualMachine"]] = relationship(back_populates="cluster")
    projects: Mapped[list["Project"]] = relationship(secondary=project_cluster_link, back_populates="clusters")


class VMFolder(Base):
    """Datacenter 하위 VM Folder. Project의 다중 선택 매칭 기준 중 하나."""

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
    projects: Mapped[list["Project"]] = relationship(secondary=project_folder_link, back_populates="folders")


class Tag(Base):
    """VM에 부여된 태그 (category+name). Project의 다중 선택 매칭 기준 중 하나."""

    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("integration_account_id", "category", "name", name="uq_tag_account_cat_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_account_id: Mapped[int] = mapped_column(ForeignKey("integration_accounts.id"), index=True)
    category: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    integration_account: Mapped["IntegrationAccount"] = relationship(back_populates="tags")
    vms: Mapped[list["VirtualMachine"]] = relationship(secondary=vm_tag_link, back_populates="tags")
    projects: Mapped[list["Project"]] = relationship(secondary=project_tag_link, back_populates="tags")

    @property
    def label(self) -> str:
        return f"{self.category}:{self.name}"


class Tenant(Base):
    """최상위 조직 단위 (고객사/사업부 등). 하위 Project/User를 소유한다."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    projects: Mapped[list["Project"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class Project(Base):
    """
    과금 단위. Cluster/VM Folder/VM Tag 중 여러 종류를 동시에, 각각 여러 값을 다중 선택해서
    매칭 기준으로 삼는다. VM은 셋 중 하나라도 일치하면(OR) 이 프로젝트에 매핑된다.

    같은 Cluster/Folder/Tag를 두 프로젝트가 동시에 선택한 경우, 수집기는 먼저 생성된(=id가
    작은) 프로젝트를 우선 매칭한다 (app/collector.py의 recompute_project_assignments 참고).
    """

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_project_tenant_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    owner_email: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenant: Mapped["Tenant"] = relationship(back_populates="projects")
    rate_card: Mapped["RateCard"] = relationship(
        back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    vms: Mapped[list["VirtualMachine"]] = relationship(back_populates="project")

    clusters: Mapped[list["Cluster"]] = relationship(secondary=project_cluster_link, back_populates="projects")
    folders: Mapped[list["VMFolder"]] = relationship(secondary=project_folder_link, back_populates="projects")
    tags: Mapped[list["Tag"]] = relationship(secondary=project_tag_link, back_populates="projects")

    @property
    def criteria_summary(self) -> str:
        """관리자 화면/결산서에 표시할 매칭 기준 요약 (예: "Cluster 2개 · Tag 1개")."""
        parts = []
        if self.clusters:
            parts.append(f"Cluster {len(self.clusters)}개")
        if self.folders:
            parts.append(f"Folder {len(self.folders)}개")
        if self.tags:
            parts.append(f"Tag {len(self.tags)}개")
        return " · ".join(parts) if parts else "미지정"


class RateCard(Base):
    """프로젝트별 현재 적용 단가. 프로젝트당 1행."""

    __tablename__ = "rate_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), unique=True)

    # 시간당 단가 (해당 통화 기준). 과금 시 블록 = rate * (COLLECTOR_INTERVAL_MINUTES/60) 로 환산.
    vcpu_rate_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    vmem_rate_per_hour_gb: Mapped[float] = mapped_column(Float, default=0.0)
    vdisk_rate_per_hour_gb: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="KRW")

    # [v3.7] 사용량 가중치 하이브리드 과금 (app/billing/engine.py 참고). 꺼져 있으면
    # (기본값) 기존과 완전히 동일하게 스펙 기준 정액으로만 계산된다. 켜면 vCPU/vMEM
    # 블록 비용에 실사용률(cpu|usage_average/mem|usage_average) 기반 가중치를 곱한다 -
    # vDisk는 가중치 대상이 아니다(할당된 스토리지 비용은 순간 IO 사용량과 무관하다고
    # 보는 것이 일반적인 클라우드 과금 관례). usage_weight_floor_pct는 사용률이
    # 0%여도 최소 이 비율만큼은 과금하는 바닥값(정액 성격을 남겨 "켜놨는데 안 쓰면 0원"이
    # 되는 것을 방지) - 100%면 사실상 순수 종량제와 동일해진다.
    usage_weight_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    usage_weight_floor_pct: Mapped[int] = mapped_column(Integer, default=30)

    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(256), default="system")

    project: Mapped["Project"] = relationship(back_populates="rate_card")


class RateCardHistory(Base):
    """단가 변경 감사 로그."""

    __tablename__ = "rate_card_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    vcpu_rate_per_hour: Mapped[float] = mapped_column(Float)
    vmem_rate_per_hour_gb: Mapped[float] = mapped_column(Float)
    vdisk_rate_per_hour_gb: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8))
    # [v3.7] RateCard와 동일한 스냅샷 필드 - 감사 로그에도 그 시점의 사용량 가중치
    # 설정이 함께 남도록 한다.
    usage_weight_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    usage_weight_floor_pct: Mapped[int] = mapped_column(Integer, default=30)
    changed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    changed_by: Mapped[str] = mapped_column(String(256), default="system")


class VirtualMachine(Base):
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

    # Project의 Cluster/Folder/Tag 다중 선택 기준에 따라 수집기가 매 주기 재계산하는 값.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)

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
    project: Mapped["Project | None"] = relationship(back_populates="vms")
    tags: Mapped[list["Tag"]] = relationship(secondary=vm_tag_link, back_populates="vms")
    samples: Mapped[list["PowerSample"]] = relationship(back_populates="vm", cascade="all, delete-orphan")


class PowerSample(Base):
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


class User(Base):
    """사용자 계정. email 필드는 실제 이메일 또는 관리자 로그인 ID("admin")를 담는
    범용 로그인 식별자로 사용한다."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER)
    # role == USER 인 경우 배정된 Tenant. role == ADMIN 인 경우 전체 Tenant 교차 조회
    # 가능하므로 NULL.
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenant: Mapped["Tenant | None"] = relationship(back_populates="users")
