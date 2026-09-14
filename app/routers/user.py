"""일반 사용자 화면용 API: 배정된 테넌트 소속 프로젝트의 리소스 사용량/요금만 조회."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.billing.aggregator import (
    available_months,
    calendar_month_period,
    compute_all_projects_usage,
    compute_month_forecast,
    compute_project_usage,
    period_over_period_change,
)
from app.billing.statement_pdf import build_project_statement_pdf
from app.database import get_db, get_ops_db
from app.models import Project, User
from app.routers.common import parse_month_param, parse_period, project_usage_to_schema
from app.schemas import AdminOverviewOut, MonthForecastOut, ProjectUsageOut

router = APIRouter(prefix="/api/me", tags=["me"])


def _require_tenant(user: User) -> int:
    if user.tenant_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "이 계정에는 배정된 테넌트가 없습니다. 관리자에게 문의하세요.",
        )
    return user.tenant_id


def _get_own_project(db: Session, user: User, project_id: int) -> Project:
    tenant_id = _require_tenant(user)
    project = db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "프로젝트를 찾을 수 없습니다")
    return project


@router.get("/overview", response_model=AdminOverviewOut)
def my_overview(
    period: str = Query("30d", description="7d | 30d | mtd | month | custom"),
    start: str | None = None,
    end: str | None = None,
    month: str | None = Query(None, description="period=month 일 때 YYYY-MM 형식"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> AdminOverviewOut:
    """배정된 테넌트 소속 전체 프로젝트(Cluster/Folder)의 사용량/요금을 조회한다."""
    tenant_id = _require_tenant(user)
    s, e = parse_period(period, start, end, month)
    results = compute_all_projects_usage(db, ops_db, s, e, tenant_id=tenant_id)
    project_schemas = [project_usage_to_schema(r, s, e) for r in results]

    currencies = {p.currency for p in project_schemas}
    if not project_schemas:
        currency_note = "등록된 프로젝트가 없습니다"
    elif len(currencies) == 1:
        currency_note = f"전체 {next(iter(currencies))} 기준 합산"
    else:
        currency_note = "프로젝트별 통화가 상이하여 합계는 참고용입니다"

    tenant_name = user.tenant.name if user.tenant else None
    total_cost = round(sum(p.total_cost for p in project_schemas), 2)
    previous_period_total_cost, period_over_period_change_pct = period_over_period_change(
        db, ops_db, s, e, total_cost, tenant_id=tenant_id
    )
    return AdminOverviewOut(
        period_start=s,
        period_end=e,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        total_projects=len(project_schemas),
        total_vms=sum(p.vm_count for p in project_schemas),
        total_powered_on_vms=sum(p.powered_on_vm_count for p in project_schemas),
        total_cost=total_cost,
        currency_note=currency_note,
        projects=project_schemas,
        previous_period_total_cost=previous_period_total_cost,
        period_over_period_change_pct=period_over_period_change_pct,
    )


@router.get("/forecast", response_model=MonthForecastOut)
def my_month_forecast(
    user: User = Depends(get_current_user), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> MonthForecastOut:
    """[v4.7] 이번 달 예상 청구액 (배정된 테넌트 범위)."""
    tenant_id = _require_tenant(user)
    result = compute_month_forecast(db, ops_db, tenant_id=tenant_id)
    return MonthForecastOut(
        month=result.month,
        mtd_total_cost=result.mtd_total_cost,
        days_elapsed=result.days_elapsed,
        days_in_month=result.days_in_month,
        forecast_total_cost=result.forecast_total_cost,
        currency_note=result.currency_note,
    )


@router.get("/projects/{project_id}/usage", response_model=ProjectUsageOut)
def my_project_usage(
    project_id: int,
    period: str = Query("30d", description="7d | 30d | mtd | month | custom"),
    start: str | None = None,
    end: str | None = None,
    month: str | None = Query(None, description="period=month 일 때 YYYY-MM 형식"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> ProjectUsageOut:
    project = _get_own_project(db, user, project_id)
    s, e = parse_period(period, start, end, month)
    result = compute_project_usage(db, ops_db, project, s, e)
    return project_usage_to_schema(result, s, e)


@router.get("/months", response_model=list[str])
def my_available_months(
    user: User = Depends(get_current_user), db: Session = Depends(get_db), ops_db: Session = Depends(get_ops_db)
) -> list[str]:
    """내 테넌트에 데이터가 존재하는 캘린더 월 목록 (YYYY-MM, 최신순)."""
    tenant_id = _require_tenant(user)
    return available_months(db, ops_db, tenant_id=tenant_id)


@router.get("/projects/{project_id}/statement.pdf")
def my_project_statement_pdf(
    project_id: int,
    month: str = Query(..., description="YYYY-MM 형식 (예: 2026-08)"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ops_db: Session = Depends(get_ops_db),
) -> Response:
    """내 테넌트 소속 프로젝트의 특정 캘린더 월 사용량 결산서를 PDF로 내려받는다."""
    project = _get_own_project(db, user, project_id)
    year, month_int = parse_month_param(month)
    s, e = calendar_month_period(year, month_int)
    result = compute_project_usage(db, ops_db, project, s, e)
    pdf_bytes = build_project_statement_pdf(result, year, month_int, s, e, tenant_name=project.tenant.name)
    filename = f"{project.key}_{year}-{month_int:02d}_statement.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
