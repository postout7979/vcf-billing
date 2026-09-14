"""라우터 공통 헬퍼: 기간 파싱, aggregator 결과 -> API 스키마 변환."""
from __future__ import annotations

import datetime as dt

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.billing.aggregator import ProjectUsageResult, calendar_month_period, default_period, month_to_date_period
from app.models import Project
from app.ops_queries import count_vms_by_project, resolve_clusters, resolve_folders, resolve_tags
from app.project_criteria import criteria_summary_from_ids, get_project_criteria_ids
from app.schemas import (
    ClusterOut,
    DailyCostOut,
    ProjectCriteriaOut,
    ProjectOut,
    ProjectUsageOut,
    RateCardOut,
    RateCardUpdate,
    TagOut,
    VmUsageOut,
    VMFolderOut,
)


def parse_month_param(month: str | None) -> tuple[int, int]:
    """"YYYY-MM" 형식의 month 쿼리 파라미터를 (year, month) 정수 튜플로 파싱한다.

    PDF 결산서 export 등 캘린더 월을 직접 다루는 엔드포인트에서 공통으로 사용한다.
    """
    if not month:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "month=YYYY-MM 파라미터가 필요합니다")
    try:
        year_str, month_str = month.split("-")
        year, month_int = int(year_str), int(month_str)
        if not 1 <= month_int <= 12:
            raise ValueError("month out of range")
        return year, month_int
    except (ValueError, IndexError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "month 형식이 올바르지 않습니다 (예: 2026-08)") from exc


def parse_period(
    period: str | None, start: str | None, end: str | None, month: str | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """
    쿼리 파라미터로부터 조회 기간(UTC datetime 튜플)을 계산한다.

    period: "7d" | "30d" | "mtd"(이번 달 1일~현재) | "month"(특정 캘린더 월 전체) | "custom"
    month: period="month" 일 때 "YYYY-MM" 형식 (HTML <input type="month"> 값과 동일한 포맷)
    custom: start/end 필수, ISO8601
    """
    if period == "mtd":
        return month_to_date_period()
    if period == "month":
        if not month:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "period=month 인 경우 month=YYYY-MM 파라미터가 필요합니다")
        year, month_int = parse_month_param(month)
        return calendar_month_period(year, month_int)
    if period == "custom" and start and end:
        s = dt.datetime.fromisoformat(start)
        e = dt.datetime.fromisoformat(end)
        if s.tzinfo is None:
            s = s.replace(tzinfo=dt.timezone.utc)
        if e.tzinfo is None:
            e = e.replace(tzinfo=dt.timezone.utc)
        return s, e
    days = 7 if period == "7d" else 30
    return default_period(days)


def project_to_out(db: Session, ops_db: Session, p: Project) -> ProjectOut:
    """Project ORM 객체를 API 응답 스키마로 변환한다 (관리자/일반 사용자 라우터 공통 사용).

    [v4.9] 매칭 기준(Cluster/VMFolder/Tag) 실제 행과 배정된 VM 개수는 Operations DB에
    있으므로, billing 세션(db)으로 기준 id를 먼저 구한 뒤 ops 세션(ops_db)에서 그 id로
    실제 행을 조회해 조합한다.
    """
    criteria_ids = get_project_criteria_ids(db, p.id)
    clusters = resolve_clusters(ops_db, criteria_ids["cluster_ids"])
    folders = resolve_folders(ops_db, criteria_ids["folder_ids"])
    tags = resolve_tags(ops_db, criteria_ids["tag_ids"])

    criteria = ProjectCriteriaOut(
        clusters=[ClusterOut(id=c.id, external_id=c.external_id, name=c.name, vm_count=len(c.vms)) for c in clusters],
        folders=[
            VMFolderOut(id=f.id, external_id=f.external_id, path=f.path, name=f.name, vm_count=len(f.vms))
            for f in folders
        ],
        tags=[TagOut(id=t.id, category=t.category, name=t.name, label=t.label) for t in tags],
    )
    vm_count = count_vms_by_project(ops_db, [p.id]).get(p.id, 0)
    return ProjectOut(
        id=p.id,
        tenant_id=p.tenant_id,
        tenant_key=p.tenant.key,
        key=p.key,
        name=p.name,
        description=p.description,
        owner_email=p.owner_email,
        criteria=criteria,
        criteria_summary=criteria_summary_from_ids(**criteria_ids),
        vm_count=vm_count,
        rate_card=RateCardOut.model_validate(p.rate_card) if p.rate_card else None,
    )


def project_usage_to_schema(result: ProjectUsageResult, start: dt.datetime, end: dt.datetime) -> ProjectUsageOut:
    vms = [
        VmUsageOut(
            vm_id=v.vm_id,
            vm_name=v.vm_name,
            vcpu_count=v.vcpu_count,
            vmem_gb=v.vmem_gb,
            vdisk_gb=v.vdisk_gb,
            powered_on_hours=v.powered_on_hours,
            uptime_ratio=v.uptime_ratio,
            vcpu_cost=round(v.vcpu_cost, 2),
            vmem_cost=round(v.vmem_cost, 2),
            vdisk_cost=round(v.vdisk_cost, 2),
            total_cost=v.total_cost,
            avg_cpu_usage_pct=v.avg_cpu_usage_pct,
            avg_mem_usage_pct=v.avg_mem_usage_pct,
        )
        for v in result.vm_results
    ]
    daily = [
        DailyCostOut(
            date=d.date,
            vcpu_cost=round(d.vcpu_cost, 2),
            vmem_cost=round(d.vmem_cost, 2),
            vdisk_cost=round(d.vdisk_cost, 2),
            total_cost=d.total_cost,
        )
        for d in result.daily_sorted
    ]
    return ProjectUsageOut(
        project_id=result.project_id,
        project_key=result.project_key,
        project_name=result.project_name,
        criteria_summary=result.criteria_summary,
        tenant_id=result.tenant_id,
        tenant_key=result.tenant_key,
        tenant_name=result.tenant_name,
        currency=result.currency,
        period_start=start,
        period_end=end,
        rate=RateCardUpdate(
            vcpu_rate_per_hour=result.rate.vcpu_rate_per_hour,
            vmem_rate_per_hour_gb=result.rate.vmem_rate_per_hour_gb,
            vdisk_rate_per_hour_gb=result.rate.vdisk_rate_per_hour_gb,
            currency=result.rate.currency,
            usage_weight_enabled=result.rate.usage_weight_enabled,
            usage_weight_floor_pct=round(result.rate.usage_weight_floor * 100),
        ),
        vm_count=result.vm_count,
        powered_on_vm_count=result.powered_on_vm_count,
        total_vcpu=result.total_vcpu,
        total_vmem_gb=result.total_vmem_gb,
        total_vdisk_gb=result.total_vdisk_gb,
        total_vcpu_cost=result.total_vcpu_cost,
        total_vmem_cost=result.total_vmem_cost,
        total_vdisk_cost=result.total_vdisk_cost,
        total_cost=result.total_cost,
        vms=vms,
        daily=daily,
    )
