"""
Billing DB 데이터 모델 (v4.9: Billing DB / Operations DB 논리 분리).

[v4.9] 이 파일은 Billing DB(과금 단위 구성)에 속하는 모델만 담는다. VM 인벤토리 수집
데이터(IntegrationAccount/VCenter/Datacenter/Cluster/VMFolder/Tag/VirtualMachine/
PowerSample)는 app/models_ops.py로 옮겨 별도 SQLAlchemy DeclarativeBase(OpsBase)를
사용한다 - 두 데이터베이스는 서로 다른 물리 DB일 수 있어(app/database.py 참고) 더 이상
하나의 Base.metadata에 같이 둘 수 없고, 그 사이에 SQLAlchemy relationship()이나 SQL
JOIN도 존재할 수 없다.

- Tenant             : 최상위 조직 단위. 하위 Project와 User를 소유한다.
- Project            : 과금 단위. Cluster/VM Folder/VM Tag 중 여러 종류를 동시에, 각각
                       여러 값을 다중 선택해서 매칭 기준으로 삼는다(OR 매칭). 실제
                       기준 id는 project_cluster_link/project_folder_link/
                       project_tag_link(아래)에 저장하며, Cluster/VMFolder/Tag 자체는
                       Operations DB에 있으므로 이 파일에서는 실제 행이 아니라 "id
                       목록"만 다룬다 (app/project_criteria.py 참고).
- RateCard/History   : 프로젝트별 단가 및 변경 이력.
- User               : 사용자 계정. role=admin 은 전체 Tenant 교차 조회, role=user 는
                       배정된 tenant_id 하나만 조회 가능. bcrypt 해시 비밀번호 사용.
- AppState           : 앱 전체 런타임 상태 싱글턴(최초 설정 게이트 등).

VirtualMachine.project_id, Project<->Cluster/Folder/Tag 매칭 기준의 cluster_id/
folder_id/tag_id는 모두 "다른 물리 DB의 행을 가리키는 정수 id"로, ForeignKey 제약을 걸지
않는다 - PostgreSQL은 물리적으로 분리된 두 데이터베이스 사이의 참조 무결성을 강제할 방법이
없다(cross-database FK 자체가 존재하지 않음). 정합성은 애플리케이션 코드(프로젝트/연동
계정 삭제 시 관련 행을 정리하는 라우터 로직, app/collector.py의 재계산)가 책임진다.
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


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


# ---------------------------------------------------------------------------
# Project의 다중 선택 매칭 기준 연결 테이블
#
# [v4.9] project_id는 Billing DB 안의 projects.id를 가리키는 실제 FK다(같은 물리 DB).
# 하지만 cluster_id/folder_id/tag_id는 Operations DB(app/models_ops.py)의
# clusters.id/vm_folders.id/tags.id를 가리키는 "소프트 참조"일 뿐이다 - 두 데이터베이스가
# 물리적으로 분리될 수 있어(app/database.py) ForeignKey()로 선언할 수 없다. 존재하지
# 않는 id를 참조하게 되는 경우(예: Operations DB 쪽에서 Cluster가 먼저 삭제됨)는
# app/collector.py의 recompute_project_assignments()가 매칭 후보에서 자연스럽게
# 제외하는 것으로 흡수한다 (조회 시 실패하지 않음).
# ---------------------------------------------------------------------------

project_cluster_link = Table(
    "project_cluster_link",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("cluster_id", Integer, primary_key=True),  # Operations DB clusters.id (소프트 참조, FK 없음)
)

project_folder_link = Table(
    "project_folder_link",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("folder_id", Integer, primary_key=True),  # Operations DB vm_folders.id (소프트 참조, FK 없음)
)

project_tag_link = Table(
    "project_tag_link",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, primary_key=True),  # Operations DB tags.id (소프트 참조, FK 없음)
)


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

    [v4.9] Cluster/VMFolder/Tag는 이제 Operations DB에 있으므로, 이 클래스는 더 이상
    `.clusters`/`.folders`/`.tags`/`.vms` relationship을 갖지 않는다. 매칭 기준 id 목록은
    app/project_criteria.py의 get_project_criteria_ids()로, 배정된 VM은
    app/ops_queries.py의 get_vms_for_project()로 조회한다.
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
    # [v4.10.1 버그 수정] RateCardHistory는 project_id를 raw FK로만 가질 뿐 이 relationship이
    # 없었다 - 그래서 프로젝트/테넌트 삭제 시 이력 행이 남아있는 채로 DELETE가 나가고,
    # PostgreSQL은 (SQLite와 달리 기본적으로 FK 제약을 강제하므로) "rate_card_history_project_id_fkey"
    # 위반으로 삭제를 거부해 관리자 화면에 Internal error가 떴다. rate_card와 동일하게
    # cascade="all, delete-orphan"을 추가해 프로젝트 삭제 시 이력도 함께 지워지도록 한다.
    rate_card_history: Mapped[list["RateCardHistory"]] = relationship(cascade="all, delete-orphan")


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


class AppState(Base):
    """[v4.8] 앱 전체에 걸친 런타임 상태 한 줄짜리 싱글턴 테이블 (id=1 고정).

    app/config.py의 Settings는 .env(컨테이너 기동 시점 환경변수) 기반이라 앱이 스스로
    "다시 보여줄지 말지" 같은 상태를 저장하기에 적합하지 않다 - 그래서 이런 값은 DB에
    별도 테이블로 둔다. 현재는 "최초 DB 설정 안내(로컬 계속 사용/외부 DB 연동)를 이미
    보여줬는지" 플래그 하나뿐이지만, 앞으로 비슷한 1회성 플래그가 늘어나면 이 테이블에
    컬럼을 추가하면 된다.
    """

    __tablename__ = "app_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    # [v4.8] "데이터베이스" 관리자 탭의 최초 설정 안내 팝업(로컬 DB 계속 사용/외부 DB
    # 연동 중 하나를 고르는 화면)을 이미 보여줬거나 admin이 건너뛰었으면 True - 이후
    # 로그인부터는 자동으로 다시 뜨지 않는다.
    initial_db_setup_seen: Mapped[bool] = mapped_column(Boolean, default=False)
