"""
DB 조회 + billing.engine 을 결합하여 프로젝트/기간 단위 사용량·요금 결과를 만든다.

사용자 화면(내 프로젝트) / 관리자 화면(전체 프로젝트) 모두 이 모듈을 통해 데이터를 얻는다.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.billing.engine import RateInput, VmUsageAccumulator, accumulate_sample, cost_of_block
from app.models import PowerSample, Project, VirtualMachine

KST = dt.timezone(dt.timedelta(hours=9))


@dataclass
class DailyCost:
    date: str  # YYYY-MM-DD (KST 기준 일자)
    vcpu_cost: float = 0.0
    vmem_cost: float = 0.0
    vdisk_cost: float = 0.0

    @property
    def total_cost(self) -> float:
        return round(self.vcpu_cost + self.vmem_cost + self.vdisk_cost, 2)


@dataclass
class ProjectUsageResult:
    project_id: int
    project_key: str
    project_name: str
    criteria_summary: str
    tenant_id: int
    tenant_key: str
    tenant_name: str
    currency: str
    rate: RateInput
    vm_results: list[VmUsageAccumulator] = field(default_factory=list)
    daily: dict[str, DailyCost] = field(default_factory=dict)

    @property
    def total_vcpu_cost(self) -> float:
        return round(sum(v.vcpu_cost for v in self.vm_results), 2)

    @property
    def total_vmem_cost(self) -> float:
        return round(sum(v.vmem_cost for v in self.vm_results), 2)

    @property
    def total_vdisk_cost(self) -> float:
        return round(sum(v.vdisk_cost for v in self.vm_results), 2)

    @property
    def total_cost(self) -> float:
        return round(self.total_vcpu_cost + self.total_vmem_cost + self.total_vdisk_cost, 2)

    @property
    def vm_count(self) -> int:
        return len(self.vm_results)

    @property
    def powered_on_vm_count(self) -> int:
        return sum(1 for v in self.vm_results if v.powered_on_blocks > 0)

    @property
    def total_vcpu(self) -> int:
        return sum(v.vcpu_count for v in self.vm_results)

    @property
    def total_vmem_gb(self) -> float:
        return round(sum(v.vmem_gb for v in self.vm_results), 1)

    @property
    def total_vdisk_gb(self) -> float:
        return round(sum(v.vdisk_gb for v in self.vm_results), 1)

    @property
    def daily_sorted(self) -> list[DailyCost]:
        return [self.daily[k] for k in sorted(self.daily.keys())]


def _rate_input(project: Project) -> RateInput:
    rc = project.rate_card
    if rc is None:
        return RateInput(0.0, 0.0, 0.0, "KRW")
    return RateInput(
        rc.vcpu_rate_per_hour,
        rc.vmem_rate_per_hour_gb,
        rc.vdisk_rate_per_hour_gb,
        rc.currency,
        usage_weight_enabled=rc.usage_weight_enabled,
        usage_weight_floor=rc.usage_weight_floor_pct / 100.0,
    )


def compute_project_usage(
    db: Session, project: Project, start: dt.datetime, end: dt.datetime
) -> ProjectUsageResult:
    rate = _rate_input(project)
    result = ProjectUsageResult(
        project_id=project.id,
        project_key=project.key,
        project_name=project.name,
        criteria_summary=project.criteria_summary,
        tenant_id=project.tenant_id,
        tenant_key=project.tenant.key,
        tenant_name=project.tenant.name,
        currency=rate.currency,
        rate=rate,
    )

    vms = db.query(VirtualMachine).filter_by(project_id=project.id).all()
    acc_by_vm = {
        vm.id: VmUsageAccumulator(
            vm_id=vm.id,
            vm_name=vm.name,
            vcpu_count=vm.vcpu_count,
            vmem_gb=vm.vmem_gb,
            vdisk_gb=vm.vdisk_gb,
        )
        for vm in vms
    }
    if not acc_by_vm:
        return result

    samples = (
        db.query(PowerSample)
        .filter(PowerSample.vm_id.in_(list(acc_by_vm.keys())))
        .filter(PowerSample.sampled_at >= start, PowerSample.sampled_at < end)
        .order_by(PowerSample.sampled_at.asc())
        .all()
    )

    for s in samples:
        acc = acc_by_vm[s.vm_id]
        state = s.power_state.value
        accumulate_sample(
            acc, state, s.vcpu_count, s.vmem_gb, s.vdisk_gb, rate, s.cpu_usage_pct, s.mem_usage_pct
        )

        if state == "on":
            day_key = s.sampled_at.astimezone(KST).strftime("%Y-%m-%d")
            daily = result.daily.setdefault(day_key, DailyCost(date=day_key))
            vcpu_cost, vmem_cost, vdisk_cost = cost_of_block(
                s.vcpu_count, s.vmem_gb, s.vdisk_gb, rate, cpu_usage_pct=s.cpu_usage_pct, mem_usage_pct=s.mem_usage_pct
            )
            daily.vcpu_cost += vcpu_cost
            daily.vmem_cost += vmem_cost
            daily.vdisk_cost += vdisk_cost

    result.vm_results = sorted(acc_by_vm.values(), key=lambda a: a.vm_name)
    return result


def compute_all_projects_usage(
    db: Session, start: dt.datetime, end: dt.datetime, tenant_id: int | None = None
) -> list[ProjectUsageResult]:
    """전체(또는 tenant_id로 필터링한) Project의 사용량을 계산한다.

    tenant_id를 지정하면 해당 Tenant 소속 Project만 반환한다 (일반 사용자의 "내 테넌트"
    조회, 또는 관리자의 특정 테넌트 교차 확인에 사용).
    """
    query = db.query(Project)
    if tenant_id is not None:
        query = query.filter(Project.tenant_id == tenant_id)
    projects = query.order_by(Project.name).all()
    return [compute_project_usage(db, p, start, end) for p in projects]


def default_period(days: int = 30) -> tuple[dt.datetime, dt.datetime]:
    """기본 조회 기간: 지금부터 과거 N일."""
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    return start, end


def month_to_date_period() -> tuple[dt.datetime, dt.datetime]:
    """이번 달(KST 기준) 1일 00:00 ~ 현재."""
    now_kst = dt.datetime.now(KST)
    start_kst = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_kst.astimezone(dt.timezone.utc), dt.datetime.now(dt.timezone.utc)


def calendar_month_period(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    """지정한 캘린더 월(KST 기준) 1일 00:00 ~ 다음 달 1일 00:00. 완료된 과거 월 조회에 사용."""
    start_kst = dt.datetime(year, month, 1, tzinfo=KST)
    if month == 12:
        end_kst = dt.datetime(year + 1, 1, 1, tzinfo=KST)
    else:
        end_kst = dt.datetime(year, month + 1, 1, tzinfo=KST)
    return start_kst.astimezone(dt.timezone.utc), end_kst.astimezone(dt.timezone.utc)


def available_months(db: Session, tenant_id: int | None = None) -> list[str]:
    """PowerSample 데이터가 존재하는 캘린더 월 목록 (YYYY-MM, 최신순). 월 선택 UI를 채우는 데 사용.

    tenant_id를 지정하면 해당 Tenant 소속 VM의 샘플만 고려한다 (일반 사용자 화면용).
    """
    query = db.query(func.min(PowerSample.sampled_at), func.max(PowerSample.sampled_at))
    if tenant_id is not None:
        query = (
            query.join(VirtualMachine, VirtualMachine.id == PowerSample.vm_id)
            .join(Project, Project.id == VirtualMachine.project_id)
            .filter(Project.tenant_id == tenant_id)
        )
    min_at, max_at = query.one()
    if min_at is None or max_at is None:
        return []
    if min_at.tzinfo is None:
        min_at = min_at.replace(tzinfo=dt.timezone.utc)
    if max_at.tzinfo is None:
        max_at = max_at.replace(tzinfo=dt.timezone.utc)

    cursor = min_at.astimezone(KST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = max_at.astimezone(KST)
    months = []
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = dt.datetime(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1, tzinfo=KST)
    return sorted(months, reverse=True)
