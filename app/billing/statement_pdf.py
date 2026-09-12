"""
프로젝트 월간 사용량 결산서(PDF) 생성.

- reportlab 로 순수하게 만든 함수 모음 (파일 I/O 없이 PDF bytes 를 반환한다 - 라우터에서
  Response(content=..., media_type="application/pdf") 로 그대로 내려보낸다)
- 한글 렌더링을 위해 나눔고딕(OFL 라이선스)을 `app/billing/fonts/`에 동봉해 임베드한다.
  (배포 환경에 한글 폰트가 없어도 항상 동일하게 렌더링되도록 하기 위함 - 시스템 폰트에 의존하지 않음)

주의: "결산서"는 내부 차지백/원가 확인용 참고 자료이며, 세금계산서 등 법적 효력이 있는
정식 계산서가 아니다 (원래 요구사항에서 실제 계산서 발행은 범위 제외). 문서 하단에 항상
이 안내 문구를 포함한다.
"""
from __future__ import annotations

import datetime as dt
import io
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.billing.aggregator import KST, ProjectUsageResult
from app.config import get_settings

_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
_FONTS_REGISTERED = False

# 라이트 테마 UI와 맞춘 팔레트
_INK = colors.HexColor("#14171B")
_MUTED = colors.HexColor("#5B6472")
_LINE = colors.HexColor("#E4E8EC")
_ACCENT = colors.HexColor("#3B54D6")
_SURFACE_2 = colors.HexColor("#F5F7F9")
_VCPU = colors.HexColor("#3B54D6")
_VMEM = colors.HexColor("#0A93A0")
_VDISK = colors.HexColor("#B0791E")


def _ensure_fonts() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont("NanumGothic", os.path.join(_FONT_DIR, "NanumGothic.ttf")))
    pdfmetrics.registerFont(TTFont("NanumGothic-Bold", os.path.join(_FONT_DIR, "NanumGothicBold.ttf")))
    _FONTS_REGISTERED = True


def _fmt_money(value: float, currency: str) -> str:
    if currency == "KRW":
        return f"₩{value:,.0f}"
    if currency == "USD":
        return f"${value:,.2f}"
    return f"{value:,.2f} {currency}"


def _fmt_num(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def _fmt_hours_minutes(hours: float) -> str:
    """소수 시간(예: 123.4)을 "123h 24m" 형태로 표시한다.

    Power-On 시간을 시간 단위로만 반올림해 보여주면(예: "123h") 5분 단위로
    누적되는 실제 값과 눈에 띄게 어긋나 보일 수 있어(예: 123시간 24분이 그냥
    "123h"로만 보이면 오차처럼 느껴짐) 분 단위까지 함께 표시한다.
    """
    total_minutes = round(max(hours, 0.0) * 60)
    h, m = divmod(total_minutes, 60)
    return f"{h}h {m}m"


def _month_label(year: int, month: int, period_start: dt.datetime, period_end: dt.datetime) -> str:
    start_kst = period_start.astimezone(KST)
    last_day_kst = period_end.astimezone(KST) - dt.timedelta(days=1)
    return f"{year}년 {month}월 ({start_kst.strftime('%Y.%m.%d')} ~ {last_day_kst.strftime('%Y.%m.%d')}, KST 기준)"


def build_project_statement_pdf(
    result: ProjectUsageResult,
    year: int,
    month: int,
    period_start: dt.datetime,
    period_end: dt.datetime,
    generated_at: dt.datetime | None = None,
    tenant_name: str | None = None,
) -> bytes:
    """지정 프로젝트/캘린더 월에 대한 사용량 결산서 PDF를 bytes로 반환한다."""
    _ensure_fonts()
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    tenant_label = tenant_name or result.tenant_name or "-"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=f"{result.project_name} {year}-{month:02d} 사용량 결산서",
        author="VCF Billing Portal",
    )

    styles = {
        "eyebrow": ParagraphStyle("eyebrow", fontName="NanumGothic", fontSize=9, textColor=_MUTED, leading=12),
        "title": ParagraphStyle("title", fontName="NanumGothic-Bold", fontSize=19, textColor=_INK, leading=24, spaceAfter=2),
        "meta": ParagraphStyle("meta", fontName="NanumGothic", fontSize=10, textColor=_MUTED, leading=14),
        "h2": ParagraphStyle("h2", fontName="NanumGothic-Bold", fontSize=12, textColor=_INK, leading=16, spaceBefore=14, spaceAfter=6),
        "cell": ParagraphStyle("cell", fontName="NanumGothic", fontSize=9.5, textColor=_INK, leading=13),
        "cellMuted": ParagraphStyle("cellMuted", fontName="NanumGothic", fontSize=8.5, textColor=_MUTED, leading=12),
        "disclaimer": ParagraphStyle("disclaimer", fontName="NanumGothic", fontSize=8, textColor=_MUTED, leading=12),
    }

    story = []

    # ---------- 헤더 ----------
    story.append(Paragraph("VCF BILLING PORTAL · 월간 사용량 결산서", styles["eyebrow"]))
    story.append(Paragraph(result.project_name, styles["title"]))
    meta_rows = [
        ["테넌트", tenant_label, "프로젝트 키", result.project_key],
        ["청구 기간", _month_label(year, month, period_start, period_end), "통화", result.currency],
        [
            "발행 일시",
            generated_at.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
            "",
            "",
        ],
    ]
    meta_table = Table(meta_rows, colWidths=[26 * mm, 90 * mm, 26 * mm, None])
    meta_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "NanumGothic-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "NanumGothic-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "NanumGothic"),
                ("FONTNAME", (3, 0), (3, -1), "NanumGothic"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("TEXTCOLOR", (0, 0), (0, -1), _MUTED),
                ("TEXTCOLOR", (2, 0), (2, -1), _MUTED),
                ("TEXTCOLOR", (1, 0), (1, -1), _INK),
                ("TEXTCOLOR", (3, 0), (3, -1), _INK),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    content_width = doc.width
    story.append(Spacer(1, 6))
    story.append(meta_table)
    story.append(Spacer(1, 4))
    story.append(_hr(content_width))

    # ---------- 요약 (KPI) ----------
    story.append(Paragraph("요약", styles["h2"]))
    kpi_labels = ["VM 수", "가동 VM", "총 vCPU", "총 vMEM", "총 vDisk", "기간 요금 합계"]
    kpi_values = [
        _fmt_num(result.vm_count),
        f"{_fmt_num(result.powered_on_vm_count)}대",
        f"{_fmt_num(result.total_vcpu)} vCPU",
        f"{_fmt_num(result.total_vmem_gb, 1)} GB",
        f"{_fmt_num(result.total_vdisk_gb, 1)} GB",
        _fmt_money(result.total_cost, result.currency),
    ]
    kpi_table = Table(
        [
            [Paragraph(v, styles["cellMuted"]) for v in kpi_labels],
            [Paragraph(f"<b>{v}</b>", styles["cell"]) for v in kpi_values],
        ],
        colWidths=[None] * 6,
    )
    kpi_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _SURFACE_2),
                ("BOX", (0, 0), (-1, -1), 0.6, _LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.6, _LINE),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
                ("TOPPADDING", (0, 1), (-1, 1), 2),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                (
                    "TEXTCOLOR",
                    (5, 1),
                    (5, 1),
                    _ACCENT,
                ),
            ]
        )
    )
    story.append(kpi_table)

    # ---------- 적용 단가 ----------
    # [v3.8] 이 텍스트가 실제 수집 간격(app/config.py의 collector_interval_minutes,
    # 기본 5분)과 무관하게 "10분"으로 하드코딩되어 있던 것을 발견해 함께 고쳤다 -
    # v3에서 과금 블록 간격이 10분 -> 5분으로 바뀐 뒤에도 이 결산서 문구는 갱신되지
    # 않아 실제 계산 방식과 어긋난 문구가 계속 나가고 있었다.
    block_minutes = get_settings().collector_interval_minutes
    story.append(Paragraph(f"적용 단가 (시간당, {block_minutes}분 단위 Power-On 시간 비례 적용)", styles["h2"]))
    rate_rows = [
        ["리소스", "vCPU / 시간", "vMEM(GB) / 시간", "vDisk(GB) / 시간"],
        [
            "단가",
            _fmt_money(result.rate.vcpu_rate_per_hour, result.currency),
            _fmt_money(result.rate.vmem_rate_per_hour_gb, result.currency),
            _fmt_money(result.rate.vdisk_rate_per_hour_gb, result.currency),
        ],
    ]
    rate_table = Table(rate_rows, colWidths=[30 * mm, None, None, None])
    rate_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "NanumGothic-Bold"),
                ("FONTNAME", (0, 1), (-1, 1), "NanumGothic"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
                ("TEXTCOLOR", (0, 1), (-1, 1), _INK),
                ("BACKGROUND", (0, 0), (-1, 0), _SURFACE_2),
                ("GRID", (0, 0), (-1, -1), 0.6, _LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    story.append(rate_table)

    # ---------- VM별 상세 ----------
    story.append(Paragraph("VM별 사용량 및 요금 상세", styles["h2"]))
    header = ["VM", "vCPU", "vMEM\n(GB)", "vDisk\n(GB)", "Power-On\n시간", "가동률", "vCPU 요금", "vMEM 요금", "vDisk 요금", "합계"]
    rows = [header]
    for v in result.vm_results:
        rows.append(
            [
                v.vm_name,
                _fmt_num(v.vcpu_count),
                _fmt_num(v.vmem_gb, 1),
                _fmt_num(v.vdisk_gb, 1),
                _fmt_hours_minutes(v.powered_on_hours),
                f"{v.uptime_ratio * 100:.0f}%",
                _fmt_money(v.vcpu_cost, result.currency),
                _fmt_money(v.vmem_cost, result.currency),
                _fmt_money(v.vdisk_cost, result.currency),
                _fmt_money(v.total_cost, result.currency),
            ]
        )
    if not result.vm_results:
        rows.append(["표시할 VM이 없습니다.", "-", "-", "-", "-", "-", "-", "-", "-", "-"])
    rows.append(
        [
            "합계",
            _fmt_num(result.total_vcpu),
            _fmt_num(result.total_vmem_gb, 1),
            _fmt_num(result.total_vdisk_gb, 1),
            "",
            "",
            _fmt_money(result.total_vcpu_cost, result.currency),
            _fmt_money(result.total_vmem_cost, result.currency),
            _fmt_money(result.total_vdisk_cost, result.currency),
            _fmt_money(result.total_cost, result.currency),
        ]
    )

    col_widths = [42 * mm, 15 * mm, 17 * mm, 17 * mm, 19 * mm, 15 * mm, 27 * mm, 27 * mm, 27 * mm, 30 * mm]
    vm_table = Table(rows, colWidths=col_widths, repeatRows=1)
    last_row = len(rows) - 1
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "NanumGothic-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "NanumGothic"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
        ("BACKGROUND", (0, 0), (-1, 0), _SURFACE_2),
        ("GRID", (0, 0), (-1, -1), 0.5, _LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        # 합계 행 강조
        ("FONTNAME", (0, last_row), (-1, last_row), "NanumGothic-Bold"),
        ("BACKGROUND", (0, last_row), (-1, last_row), _SURFACE_2),
        ("LINEABOVE", (0, last_row), (-1, last_row), 1, _INK),
        ("TEXTCOLOR", (-1, last_row), (-1, last_row), _ACCENT),
    ]
    # 짝수 VM 행 zebra striping (헤더/합계 제외)
    for i in range(1, last_row):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FAFBFC")))
    vm_table.setStyle(TableStyle(style))
    story.append(vm_table)

    story.append(Spacer(1, 14))
    story.append(_hr(content_width))
    story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            f"본 문서는 VCF Operations에서 수집한 리소스 사용량(Power-On 시간, {block_minutes}분 단위 집계)을 기준으로 "
            "자동 산출된 <b>사용량 결산 참고자료</b>이며, 세금계산서 등 법적 효력이 있는 정식 계산서가 아닙니다. "
            "단가는 조회 시점 기준으로 조회 기간 전체에 일괄 적용되었습니다.",
            styles["disclaimer"],
        )
    )

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("NanumGothic", 8)
        canvas.setFillColor(_MUTED)
        page_w = landscape(A4)[0]
        canvas.drawString(16 * mm, 9 * mm, f"VCF Billing Portal · {result.project_key} · {year}-{month:02d}")
        canvas.drawRightString(page_w - 16 * mm, 9 * mm, f"{doc_.page} 페이지")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _hr(width):
    t = Table([[""]], colWidths=[width], rowHeights=[0.6])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, _LINE), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t
