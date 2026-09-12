"""API 요청/응답 Pydantic 스키마."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str  # 실제 이메일 또는 관리자 로그인 ID("admin")
    password: str


class UserOut(BaseModel):
    email: str
    display_name: str
    role: str
    tenant_id: int | None = None
    tenant_key: str | None = None
    tenant_name: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class RateCardOut(BaseModel):
    vcpu_rate_per_hour: float
    vmem_rate_per_hour_gb: float
    vdisk_rate_per_hour_gb: float
    currency: str
    # [v3.7] 사용량 가중치 하이브리드 과금 설정. usage_weight_enabled=False(기본값)면
    # 기존과 완전히 동일한 스펙 기준 정액 과금이다. app/billing/engine.py 참고.
    usage_weight_enabled: bool = False
    usage_weight_floor_pct: int = 30
    updated_at: dt.datetime
    updated_by: str

    model_config = {"from_attributes": True}


class RateCardUpdate(BaseModel):
    vcpu_rate_per_hour: float = Field(ge=0)
    vmem_rate_per_hour_gb: float = Field(ge=0)
    vdisk_rate_per_hour_gb: float = Field(ge=0)
    currency: str = "KRW"
    usage_weight_enabled: bool = False
    usage_weight_floor_pct: int = Field(default=30, ge=0, le=100)


# --------------------------------------------------------------------------
# Tenant
# --------------------------------------------------------------------------


class TenantCreate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


class TenantUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class TenantOut(BaseModel):
    id: int
    key: str
    name: str
    description: str
    project_count: int
    user_count: int
    created_at: dt.datetime


# --------------------------------------------------------------------------
# 연동 계정 (VCF Operations / Aria Operations) - 독립/전역 엔티티
# --------------------------------------------------------------------------


class IntegrationAccountCreate(BaseModel):
    kind: str = Field(description='"vcf_ops" 또는 "aria_ops"')
    name: str = Field(min_length=1, max_length=128, description="관리자 화면에 표시할 연동 계정 이름")
    base_url: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    auth_source: str = "local"
    verify_ssl: bool = True


class IntegrationAccountUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = Field(default=None, description="입력 시에만 변경, 비워두면 기존 비밀번호 유지")
    auth_source: str | None = None
    verify_ssl: bool | None = None


class IntegrationAccountOut(BaseModel):
    id: int
    kind: str
    name: str
    base_url: str
    username: str
    auth_source: str
    verify_ssl: bool
    is_mock: bool
    vcenter_count: int
    vm_count: int
    updated_at: dt.datetime
    updated_by: str
    # 비밀번호는 절대 응답에 포함하지 않는다.

    # 연동 상태 - "never"(한 번도 시도 안 함) | "success" | "error". 계정 등록/수정 시 및
    # "가져오기" 버튼, 5분 주기 자동 수집이 끝날 때마다 갱신된다.
    last_sync_status: str
    last_sync_at: dt.datetime | None
    last_sync_error: str | None
    last_sync_vm_count: int


class IntegrationSyncOut(BaseModel):
    """"가져오기"(수동 즉시 수집) 또는 계정 생성/수정 시 자동 연동 시도의 결과."""

    account: IntegrationAccountOut
    status: str  # "success" | "error"
    message: str
    new_sample_count: int


# --------------------------------------------------------------------------
# 인벤토리 (연동 계정 하위 vCenter/Datacenter/Cluster/VM Folder/Tag 계층)
# --------------------------------------------------------------------------


class TagOut(BaseModel):
    id: int
    category: str
    name: str
    label: str


class ClusterOut(BaseModel):
    id: int
    external_id: str
    name: str
    vm_count: int


class VMFolderOut(BaseModel):
    id: int
    external_id: str
    path: str
    name: str
    vm_count: int


class DatacenterOut(BaseModel):
    id: int
    external_id: str
    name: str
    clusters: list[ClusterOut] = []
    folders: list[VMFolderOut] = []


class VCenterOut(BaseModel):
    id: int
    external_id: str
    name: str
    datacenters: list[DatacenterOut] = []


class IntegrationInventoryOut(BaseModel):
    """연동 계정 하위 전체 인벤토리 트리. 프로젝트 생성/수정 화면에서 Cluster/VM Folder/
    VM Tag 다중 선택 체크박스를 채우는 데 사용한다."""

    integration_account_id: int
    integration_account_name: str
    vcenters: list[VCenterOut] = []
    tags: list[TagOut] = []
    vm_count: int


# --------------------------------------------------------------------------
# Project (과금 단위) - Cluster/VM Folder/VM Tag 다중 선택 매칭 기준(OR)
# --------------------------------------------------------------------------


class ProjectCriteriaIn(BaseModel):
    """Project 생성/수정 시 지정하는 매칭 기준. 세 종류를 동시에 조합할 수 있으며(OR 매칭),
    각 종류마다 여러 값을 선택할 수 있다. id는 /inventory 응답에서 얻은 것을 그대로 사용한다."""

    cluster_ids: list[int] = []
    folder_ids: list[int] = []
    tag_ids: list[int] = []


class ProjectCreate(ProjectCriteriaIn):
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    owner_email: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    owner_email: str | None = None
    cluster_ids: list[int] | None = None
    folder_ids: list[int] | None = None
    tag_ids: list[int] | None = None


class ProjectCriteriaOut(BaseModel):
    clusters: list[ClusterOut] = []
    folders: list[VMFolderOut] = []
    tags: list[TagOut] = []


class ProjectOut(BaseModel):
    id: int
    tenant_id: int
    tenant_key: str
    key: str
    name: str
    description: str
    owner_email: str
    criteria: ProjectCriteriaOut
    criteria_summary: str
    vm_count: int
    rate_card: RateCardOut | None = None


class UserCreate(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=4)
    display_name: str = ""
    role: str = "user"  # "user" | "admin"


class UserPasswordUpdate(BaseModel):
    password: str = Field(min_length=4)


class UserAdminOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: str
    tenant_id: int | None = None

    model_config = {"from_attributes": True}


class TenantDetailOut(TenantOut):
    projects: list[ProjectOut] = []
    users: list[UserAdminOut] = []


# --------------------------------------------------------------------------
# 사용량 / 요금
# --------------------------------------------------------------------------


class VmUsageOut(BaseModel):
    vm_id: int
    vm_name: str
    vcpu_count: int
    vmem_gb: float
    vdisk_gb: float
    powered_on_hours: float
    uptime_ratio: float
    vcpu_cost: float
    vmem_cost: float
    vdisk_cost: float
    total_cost: float
    # [v3.7] 기간 내 Power-On 블록의 평균 실사용률(%). 수집 실패/미지원 환경/가중치
    # 미사용 등으로 값을 하나도 못 구했으면 None(0%가 아님 - "0"과 "모름"을 구분).
    avg_cpu_usage_pct: float | None = None
    avg_mem_usage_pct: float | None = None


class DailyCostOut(BaseModel):
    date: str
    vcpu_cost: float
    vmem_cost: float
    vdisk_cost: float
    total_cost: float


class ProjectUsageOut(BaseModel):
    project_id: int
    project_key: str
    project_name: str
    criteria_summary: str
    tenant_id: int
    tenant_key: str
    tenant_name: str
    currency: str
    period_start: dt.datetime
    period_end: dt.datetime
    rate: RateCardUpdate
    vm_count: int
    powered_on_vm_count: int
    total_vcpu: int
    total_vmem_gb: float
    total_vdisk_gb: float
    total_vcpu_cost: float
    total_vmem_cost: float
    total_vdisk_cost: float
    total_cost: float
    vms: list[VmUsageOut]
    daily: list[DailyCostOut]


class AdminOverviewOut(BaseModel):
    period_start: dt.datetime
    period_end: dt.datetime
    tenant_id: int | None = None
    tenant_name: str | None = None
    total_projects: int
    total_vms: int
    total_powered_on_vms: int
    total_cost: float
    currency_note: str
    projects: list[ProjectUsageOut]
