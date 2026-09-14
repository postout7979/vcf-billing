"""
DB 조회 + billing.engine 을 결합하여 프로젝트/기간 단위 사용량·요금 결과를 만든다.

사용자 화면(내 프로젝트) / 관리자 화면(전체 프로젝트) 모두 이 모듈을 통해 데이터를 얻는다.

[v4.9] Project/RateCard(Billing DB)와 VirtualMachine/PowerSample(Operations DB)가
서로 다른 물리 데이터베이스일 수 있게 되면서, 이 모듈의 모든 함수는 billing 세션(db)에
더해 ops 세션(ops_db)을 함께 받는다. 예전에는 `project.vms`(ORM relationship)로 VM을
얻었지만, 이제는 app/ops_queries.get_vms_for_project()로 Operations DB에서 직접
조회한다 - PowerSample 조회 자체(VM->PowerSample)는 둘 다 Operations DB에 있는 정상
FK 관계라 그대로다.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.billing.engine import RateInput, VmUsageAccumulator, accumulate_sample, cost_of_block
from app.models import Project
from app.models_ops import PowerSample, VirtualMachine
from app.ops_queries import get_vms_for_project
from app.project_criteria import criteria_summary_from_ids, get_project_criteria_ids

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
    db: Session, ops_db: Session, project: Project, start: dt.datetime, end: dt.datetime
) -> ProjectUsageResult:
    rate = _rate_input(project)
    criteria_ids = get_project_criteria_ids(db, project.id)
    result = ProjectUsageResult(
        project_id=project.id,
        project_key=project.key,
        project_name=project.name,
        criteria_summary=criteria_summary_from_ids(**criteria_ids),
        tenant_id=project.tenant_id,
        tenant_key=project.tenant.key,
        tenant_name=project.tenant.name,
        currency=rate.currency,
        rate=rate,
    )

    vms = get_vms_for_project(ops_db, project.id)
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
        ops_db.query(PowerSample)
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
    db: Session, ops_db: Session, start: dt.datetime, end: dt.datetime, tenant_id: int | None = None
) -> list[ProjectUsageResult]:
    """전체(또는 tenant_id로 필터링한) Project의 사용량을 계산한다.

    tenant_id를 지정하면 해당 Tenant 소속 Project만 반환한다 (일반 사용자의 "내 테넌트"
    조회, 또는 관리자의 특정 테넌트 교차 확인에 사용). 이 앱의 규모(design.md 기준
    "수십~수백 VM")에서는 프로젝트마다 compute_project_usage() 1회 호출로 충분하다는
    것이 기존 설계 결정이며, Operations DB 분리 이후에도 그대로 유지한다.
    """
    query = db.query(Project)
    if tenant_id is not None:
        query = query.filter(Project.tenant_id == tenant_id)
    projects = query.order_by(Project.name).all()
    return [compute_project_usage(db, ops_db, p, start, end) for p in projects]


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


def _currency_note(results: list[ProjectUsageResult]) -> str:
    currencies = {r.currency for r in results}
    if not currencies:
        return "-"
    return next(iter(currencies)) if len(currencies) == 1 else "혼합"


def total_cost_for_range(
    db: Session, ops_db: Session, start: dt.datetime, end: dt.datetime, tenant_id: int | None = None
) -> float:
    """[v4.7] 지정 기간 전체(또는 tenant_id로 필터링한) 프로젝트 총 요금 합계만 필요할 때 쓰는
    가벼운 헬퍼 - "전월/직전 기간 대비 증감" 계산처럼 VM별 상세 없이 합계 한 줄만 있으면 될 때
    compute_all_projects_usage()를 그대로 호출하되 합산까지 여기서 끝낸다."""
    results = compute_all_projects_usage(db, ops_db, start, end, tenant_id=tenant_id)
    return round(sum(r.total_cost for r in results), 2)


def period_over_period_change(
    db: Session,
    ops_db: Session,
    start: dt.datetime,
    end: dt.datetime,
    current_total_cost: float,
    tenant_id: int | None = None,
) -> tuple[float, float | None]:
    """[v4.7] "전월 대비 증감" - 현재 조회 중인 기간과 정확히 같은 길이의 직전 기간을 비교한다.

    캘린더 월 경계를 특별 취급하지 않고 "현재 기간 길이만큼 그 직전"을 항상 비교 대상으로 삼는다
    (period=7d/30d/mtd/month/custom 어떤 모드든 동일한 방식으로 일관되게 동작하도록 하기 위한
    설계 결정 - 예: period=month로 특정 캘린더 월을 조회 중이면 그 직전 같은 일수만큼이 비교
    대상이 되어, 대부분의 경우 사실상 "전월"과 거의 같지만 월별 일수 차이(28~31일)만큼은
    정확히 전월 1일~말일과 일치하지 않을 수 있다).

    반환값: (직전 기간 총 요금, 증감률(%) - 직전 기간 요금이 0이면 나눗셈이 무의미해 None)
    """
    duration = end - start
    prev_start = start - duration
    prev_end = start
    previous_total_cost = total_cost_for_range(db, ops_db, prev_start, prev_end, tenant_id=tenant_id)
    if previous_total_cost > 0:
        change_pct = round((current_total_cost - previous_total_cost) / previous_total_cost * 100, 1)
    else:
        change_pct = None  # 직전 기간에 사용량/요금이 전혀 없었으면 증감률 자체가 정의되지 않음
    return previous_total_cost, change_pct


@dataclass
class MonthForecastResult:
    """[v4.7] "이번 달 예상 청구액" - 이번 달 1일부터 지금까지의 실적을 하루 평균으로 환산해
    이번 달 전체 일수에 곱한 단순 run-rate 추정치. 선택된 조회 기간(period 파라미터)과 무관하게
    항상 "지금 이 순간의 캘린더 월" 기준으로 계산한다 - 사용자가 예를 들어 "최근 7일" 화면을
    보고 있어도 "이번 달 예상 청구액"은 별개로 항상 확인할 수 있어야 하기 때문."""

    month: str  # "YYYY-MM" (KST 기준)
    mtd_total_cost: float
    days_elapsed: float
    days_in_month: int
    forecast_total_cost: float
    currency_note: str


def compute_month_forecast(db: Session, ops_db: Session, tenant_id: int | None = None) -> MonthForecastResult:
    now_kst = dt.datetime.now(KST)
    month_start_kst = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc = month_start_kst.astimezone(dt.timezone.utc)
    end_utc = dt.datetime.now(dt.timezone.utc)

    results = compute_all_projects_usage(db, ops_db, start_utc, end_utc, tenant_id=tenant_id)
    mtd_total_cost = round(sum(r.total_cost for r in results), 2)
    days_in_month = calendar.monthrange(now_kst.year, now_kst.month)[1]
    days_elapsed = max((now_kst - month_start_kst).total_seconds() / 86400.0, 0.0)

    # 월 시작 직후(예: 반나절도 안 지난 시점)에는 하루 평균이 극단적으로 튀어 추정치가
    # 왜곡되므로, 최소 반나절(0.5일)이 지나기 전까지는 예측 대신 지금까지의 실적 그대로를
    # "예상액"으로 보여준다(=아직 추정할 근거가 부족함을 값 자체로 나타냄).
    if days_elapsed < 0.5:
        forecast_total_cost = mtd_total_cost
    else:
        forecast_total_cost = round(mtd_total_cost / days_elapsed * days_in_month, 2)

    return MonthForecastResult(
        month=now_kst.strftime("%Y-%m"),
        mtd_total_cost=mtd_total_cost,
        days_elapsed=round(days_elapsed, 2),
        days_in_month=days_in_month,
        forecast_total_cost=forecast_total_cost,
        currency_note=_currency_note(results),
    )


def available_months(db: Session, ops_db: Session, tenant_id: int | None = None) -> list[str]:
    """PowerSample 데이터가 존재하는 캘린더 월 목록 (YYYY-MM, 최신순). 월 선택 UI를 채우는 데 사용.

    tenant_id를 지정하면 해당 Tenant 소속 VM의 샘플만 고려한다 (일반 사용자 화면용).

    [v4.9] Project(tenant 소속 여부)는 Billing DB, VirtualMachine/PowerSample은
    Operations DB에 있어 더 이상 하나의 쿼리로 JOIN할 수 없다 - tenant_id가 지정되면
    먼저 Billing DB에서 해당 테넌트의 project id 목록을 구하고, 그 project_id로
    Operations DB의 VM을 필터링한다.
    """
    query = ops_db.query(func.min(PowerSample.sampled_at), func.max(PowerSample.sampled_at))
    if tenant_id is not None:
        project_ids = [pid for (pid,) in db.query(Project.id).filter(Project.tenant_id == tenant_id).all()]
        if not project_ids:
            return []
        query = query.join(VirtualMachine, VirtualMachine.id == PowerSample.vm_id).filter(
            VirtualMachine.project_id.in_(project_ids)
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
