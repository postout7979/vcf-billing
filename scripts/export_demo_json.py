"""
스탠드얼론 웹 UI 데모(아티팩트)용 데이터 내보내기.

기존 billing 엔진/aggregator를 그대로 사용해 실제 계산 로직으로 산출한
결과를 JSON으로 덤프한다. (프론트엔드 전용 데모에서 재계산 로직을 다시
구현하지 않고, 이미 검증된 파이썬 엔진의 산출값을 그대로 신뢰하기 위함)
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json

from app.billing.aggregator import (
    available_months,
    calendar_month_period,
    compute_all_projects_usage,
    compute_project_usage,
    default_period,
    month_to_date_period,
)
from app.database import OpsSessionLocal, SessionLocal
from app.models import User


def _vm_to_dict(v):
    return {
        "vm_id": v.vm_id,
        "vm_name": v.vm_name,
        "vcpu_count": v.vcpu_count,
        "vmem_gb": v.vmem_gb,
        "vdisk_gb": v.vdisk_gb,
        "powered_on_hours": v.powered_on_hours,
        "uptime_ratio": v.uptime_ratio,
        "vcpu_cost": round(v.vcpu_cost, 2),
        "vmem_cost": round(v.vmem_cost, 2),
        "vdisk_cost": round(v.vdisk_cost, 2),
        "total_cost": v.total_cost,
    }


def _project_to_dict(result, s, e):
    return {
        "project_id": result.project_id,
        "project_key": result.project_key,
        "project_name": result.project_name,
        "currency": result.currency,
        "period_start": s.isoformat(),
        "period_end": e.isoformat(),
        "rate": {
            "vcpu_rate_per_hour": result.rate.vcpu_rate_per_hour,
            "vmem_rate_per_hour_gb": result.rate.vmem_rate_per_hour_gb,
            "vdisk_rate_per_hour_gb": result.rate.vdisk_rate_per_hour_gb,
            "currency": result.rate.currency,
        },
        "vm_count": result.vm_count,
        "powered_on_vm_count": result.powered_on_vm_count,
        "total_vcpu": result.total_vcpu,
        "total_vmem_gb": result.total_vmem_gb,
        "total_vdisk_gb": result.total_vdisk_gb,
        "total_vcpu_cost": result.total_vcpu_cost,
        "total_vmem_cost": result.total_vmem_cost,
        "total_vdisk_cost": result.total_vdisk_cost,
        "total_cost": result.total_cost,
        "vms": [_vm_to_dict(v) for v in result.vm_results],
        "daily": [dataclasses.asdict(d) | {"total_cost": d.total_cost} for d in result.daily_sorted],
    }


def main():
    # [v4.9] Billing DB(Project/RateCard/User)와 Operations DB(VM/PowerSample)가
    # 분리되면서 aggregator 호출에는 두 세션이 모두 필요하다.
    db = SessionLocal()
    ops_db = OpsSessionLocal()
    out = {"periods": {}}

    for period_key, days in [("7d", 7), ("30d", 30)]:
        s, e = default_period(days)
        projects = compute_all_projects_usage(db, ops_db, s, e)
        project_dicts = [_project_to_dict(p, s, e) for p in projects]
        out["periods"][period_key] = {
            "period_start": s.isoformat(),
            "period_end": e.isoformat(),
            "projects": project_dicts,
        }

    # 이번 달(1일~오늘)
    s, e = month_to_date_period()
    projects = compute_all_projects_usage(db, ops_db, s, e)
    out["periods"]["mtd"] = {
        "period_start": s.isoformat(),
        "period_end": e.isoformat(),
        "projects": [_project_to_dict(p, s, e) for p in projects],
    }

    # 데이터가 존재하는 캘린더 월 전체 (월 선택 드롭다운용)
    months = available_months(db, ops_db)
    for ym in months:
        year_str, month_str = ym.split("-")
        s, e = calendar_month_period(int(year_str), int(month_str))
        projects = compute_all_projects_usage(db, ops_db, s, e)
        out["periods"][f"month:{ym}"] = {
            "period_start": s.isoformat(),
            "period_end": e.isoformat(),
            "projects": [_project_to_dict(p, s, e) for p in projects],
        }
    out["months"] = months

    users = db.query(User).order_by(User.role.desc(), User.email).all()
    out["users"] = [
        {
            "email": u.email,
            "display_name": u.display_name,
            "role": u.role.value,
            "tenant_key": u.tenant.key if u.tenant_id else None,
        }
        for u in users
    ]

    db.close()
    ops_db.close()

    with open("/root/vcf-billing-portal/demo_data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=None, separators=(",", ":"))

    print("exported", len(out["periods"]["30d"]["projects"]), "projects")


if __name__ == "__main__":
    main()
