"""[v4.10] "테넌트/프로젝트/과금내역(과금 원천 데이터)" 백업 & 복구.

배경 (설계 결정, claude/vcf-billing-portal-design.md "v4.10 개편" 참고)
----------------------------------------------------------------------
v4.9에서 Billing DB(Tenant/Project/RateCard/User)와 Operations DB(IntegrationAccount~
VirtualMachine/PowerSample)를 물리적으로 분리할 수 있게 했는데, 다시 생각해보니 "신규
배포(재설치) 시 테넌트/프로젝트/과금내역을 잃지 않는 것"이 목표라면 두 DB를 별도로
백업/복구하는 것보다 "논리적으로 하나의 백업 단위"로 묶는 편이 낫다 - 특히 과금 계산의
원천 데이터인 PowerSample(VM 전원상태 스냅샷)은 Operations DB에 있지만, 이 데이터가
없으면 과거 과금액을 다시는 재계산할 수 없는 반면(재수집은 "현재" 상태만 갱신할 뿐 과거를
복원하지 못함), Cluster/VMFolder/Tag 같은 인벤토리 메타데이터는 연동 계정을 다시 연결하면
언제든 재수집으로 복구된다. 그래서 v4.9의 DB 분리 아키텍처 자체는 유지하되(architecture),
백업/복구 "기능"은 두 DB를 한 파일로 묶어 다룬다.

핵심 설계 결정
--------------
1. **분리 구조는 유지, 백업/복구만 통합**: Billing DB와 Operations DB는 여전히 서로 다른
   물리 DB일 수 있다. 이 모듈은 호출부(라우터)로부터 두 세션을 모두 받아, 하나의 백업
   파일(JSON+gzip)에 두 DB의 데이터를 함께 담고, 복구 시에도 하나의 파일에서 두 DB로
   함께 되돌린다.
2. **연동 계정 접속정보는 제외**: `IntegrationAccount.base_url/username/password_encrypted`
   는 백업에 담지 않는다(사용자 결정). 다만 그 계정 행 자체(id/name/kind 등 식별 정보)는
   백업에 포함한다 - VCenter/Cluster/VMFolder/Tag/VirtualMachine이 전부 이 행을
   외래키로 참조하고 있어, 행 자체가 없으면 복구를 아예 할 수 없기 때문이다. 복구 시
   이 행은 `needs_reconnect=True`로 표시되고 접속정보는 빈 값으로 채워진다 - 관리자가
   "계정 연동" 화면에서 실제 접속정보를 입력해 저장하면(기존 PUT 흐름 그대로) 같은
   id를 유지한 채 정상 계정으로 전환되고, 이미 복구되어 있던 VM/PowerSample도 다음
   수집 주기부터 같은 행이 그대로 갱신(upsert)된다 - 별도 재연결/재매칭 로직이 필요
   없다(app/models_ops.py의 needs_reconnect 필드 주석 참고).
3. **원시 테이블 단위 raw dump/restore**: ORM 객체 그래프가 아니라 SQLAlchemy Core로
   테이블별 전체 행을 그대로 JSON 직렬화한다 - PK 값을 그대로 보존해야 소프트 참조
   (project_cluster_link의 cluster_id 등, FK가 아니라 정수 id로만 연결됨)와 진짜 FK
   양쪽이 복구 후에도 깨지지 않는다.
4. **복구는 항상 파괴적 전체 교체**: "신규 배포 시 복구"가 주 사용 시나리오이므로, 복구는
   대상 DB(Billing/Operations 양쪽)의 대상 테이블을 모두 비운 뒤 백업 내용으로 다시
   채우는 전체 교체 방식이다(부분 병합 아님). app_state(설정 마법사 노출 여부 플래그)는
   백업/복구 대상이 아니다 - 사용자 데이터가 아니라 순수 UI 상태이기 때문이다.
"""
from __future__ import annotations

import datetime as dt
import enum
import gzip
import json
from dataclasses import dataclass

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, MetaData, Table, delete, func, insert, select, text
from sqlalchemy.orm import Session

from app.database import Base, OpsBase, engine as billing_engine, ops_engine
import app.models  # noqa: F401  (Base.metadata 등록)
import app.models_ops  # noqa: F401  (OpsBase.metadata 등록)

BACKUP_FORMAT = "vcf-billing-backup"
BACKUP_FORMAT_VERSION = 1

# [v4.10] 백업 대상 테이블 목록 - "백업 대상 리스트"(사용자에게 알려야 하는 항목)의
# 근거가 되는 단일 소스. Base.metadata.sorted_tables/OpsBase.metadata.sorted_tables 전체를
# 무조건 쓰지 않고 명시적으로 나열한 이유는, 앞으로 테이블이 추가될 때 "백업에 포함할지"를
# 항상 의식적으로 결정하게 하기 위함이다 (app_state처럼 사용자 데이터가 아닌 테이블은
# 의도적으로 제외).
BILLING_TABLES = [
    "tenants",
    "users",
    "projects",
    "rate_cards",
    "rate_card_history",
    "project_cluster_link",
    "project_folder_link",
    "project_tag_link",
]

OPERATIONS_TABLES = [
    "integration_accounts",
    "vcenters",
    "datacenters",
    "clusters",
    "vm_folders",
    "tags",
    "virtual_machines",
    "vm_tag_link",
    "power_samples",
]

# 화면/보고용 한글 표시 이름 - "백업 대상 리스트"를 사용자에게 보여줄 때 사용한다.
TABLE_LABELS_KO: dict[str, str] = {
    "tenants": "테넌트",
    "users": "사용자 계정",
    "projects": "프로젝트",
    "rate_cards": "프로젝트별 현재 단가",
    "rate_card_history": "단가 변경 이력",
    "project_cluster_link": "프로젝트-Cluster 매칭 기준",
    "project_folder_link": "프로젝트-VM Folder 매칭 기준",
    "project_tag_link": "프로젝트-VM Tag 매칭 기준",
    "integration_accounts": "연동 계정 (접속정보 제외 - 이름/식별자만)",
    "vcenters": "vCenter 인벤토리",
    "datacenters": "Datacenter 인벤토리",
    "clusters": "Cluster 인벤토리",
    "vm_folders": "VM Folder 인벤토리",
    "tags": "VM Tag 인벤토리",
    "virtual_machines": "VM 인벤토리(현재 스펙)",
    "vm_tag_link": "VM-Tag 연결",
    "power_samples": "VM 전원상태 이력 (과금 원천 데이터)",
}

# 연동 계정의 접속정보 컬럼 - 백업에서 제외(빈 값으로 대체)한다.
_INTEGRATION_ACCOUNT_REDACT_DEFAULTS: dict[str, object] = {
    "base_url": "",
    "username": "",
    "password_encrypted": "",
    "auth_source": "local",
    "verify_ssl": True,
    "last_sync_status": "never",
    "last_sync_at": None,
    "last_sync_error": None,
    "last_sync_vm_count": 0,
    "needs_reconnect": True,
}


class BackupFormatError(ValueError):
    """백업 파일 형식/버전이 올바르지 않을 때."""


class RestoreConfirmationError(ValueError):
    """복구 확인(confirm) 플래그가 누락되었을 때."""


@dataclass(frozen=True)
class BackupTarget:
    label: str  # "billing" | "operations"
    metadata: MetaData
    table_names: list[str]


def _billing_backup_target() -> BackupTarget:
    return BackupTarget(label="billing", metadata=Base.metadata, table_names=BILLING_TABLES)


def _operations_backup_target() -> BackupTarget:
    return BackupTarget(label="operations", metadata=OpsBase.metadata, table_names=OPERATIONS_TABLES)


def _table(target: BackupTarget, name: str) -> Table:
    return target.metadata.tables[name]


def _serialize_value(value: object) -> object:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    return value


def _deserialize_value(value: object, column) -> object:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(column.type, SAEnum) and getattr(column.type, "enum_class", None) is not None:
        return column.type.enum_class(value)
    if isinstance(column.type, Boolean):
        return bool(value)
    return value


def _dump_table(session: Session, table: Table, redact: dict[str, object] | None = None) -> list[dict]:
    rows = session.execute(select(table)).mappings().all()
    out: list[dict] = []
    for row in rows:
        record = {col.name: _serialize_value(row[col.name]) for col in table.columns}
        if redact:
            record.update(redact)
        out.append(record)
    return out


def build_backup_manifest(billing_db: Session, ops_db: Session) -> list[dict]:
    """"백업 대상 리스트" 화면에 보여줄, 현재 시점 기준 테이블별 대상/행수 목록."""
    manifest: list[dict] = []
    for name in BILLING_TABLES:
        table = _table(_billing_backup_target(), name)
        count = billing_db.scalar(select(func.count()).select_from(table)) or 0
        manifest.append({"database": "billing", "table": name, "label": TABLE_LABELS_KO.get(name, name), "row_count": count})
    for name in OPERATIONS_TABLES:
        table = _table(_operations_backup_target(), name)
        count = ops_db.scalar(select(func.count()).select_from(table)) or 0
        manifest.append({"database": "operations", "table": name, "label": TABLE_LABELS_KO.get(name, name), "row_count": count})
    return manifest


def export_backup_bundle(billing_db: Session, ops_db: Session) -> bytes:
    """Billing DB + Operations DB의 백업 대상 테이블을 하나의 gzip JSON 파일로 묶는다."""
    billing_target = _billing_backup_target()
    ops_target = _operations_backup_target()

    billing_data: dict[str, list[dict]] = {}
    for name in BILLING_TABLES:
        billing_data[name] = _dump_table(billing_db, _table(billing_target, name))

    ops_data: dict[str, list[dict]] = {}
    for name in OPERATIONS_TABLES:
        redact = _INTEGRATION_ACCOUNT_REDACT_DEFAULTS if name == "integration_accounts" else None
        ops_data[name] = _dump_table(ops_db, _table(ops_target, name), redact=redact)

    bundle = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": (
            "이 백업 파일은 VCF Operations 연동 계정의 접속정보(URL/계정명/암호화된 비밀번호)를 "
            "포함하지 않습니다 - 복구 후 관리자가 '계정 연동' 화면에서 다시 입력해야 합니다."
        ),
        "billing": billing_data,
        "operations": ops_data,
    }
    payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    return gzip.compress(payload)


def backup_filename() -> str:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"vcf-billing-backup-{timestamp}.json.gz"


def parse_backup_bundle(raw: bytes) -> dict:
    """업로드된 백업 파일(bytes)을 검증하고 파싱한다. gzip이 아니어도(평문 JSON) 허용한다."""
    try:
        payload = gzip.decompress(raw)
    except OSError:
        payload = raw
    try:
        bundle = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupFormatError("백업 파일을 읽을 수 없습니다 - 올바른 백업 파일(.json.gz)인지 확인하세요") from exc

    if not isinstance(bundle, dict) or bundle.get("format") != BACKUP_FORMAT:
        raise BackupFormatError("이 파일은 VCF Billing Portal 백업 파일 형식이 아닙니다")
    if bundle.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupFormatError(
            f"지원하지 않는 백업 파일 버전입니다 (파일 버전: {bundle.get('format_version')}, "
            f"현재 앱이 지원하는 버전: {BACKUP_FORMAT_VERSION})"
        )
    if "billing" not in bundle or "operations" not in bundle:
        raise BackupFormatError("백업 파일 내용이 손상되었습니다 (billing/operations 섹션 누락)")
    return bundle


def _reset_pg_sequence(conn, table: Table) -> None:
    auto_col = table._autoincrement_column
    if auto_col is None:
        return
    seq_name = f"{table.name}_{auto_col.name}_seq"
    conn.execute(
        text(
            f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({auto_col.name}) FROM {table.name}), 1), "
            f"(SELECT MAX({auto_col.name}) FROM {table.name}) IS NOT NULL)"
        )
    )


def _restore_target(
    session: Session, engine, target: BackupTarget, data: dict[str, list[dict]], is_postgres: bool
) -> dict[str, int]:
    result_counts: dict[str, int] = {}

    # 대상 테이블을 FK 의존 역순으로 먼저 전부 비운다 (백업 대상 테이블 = 이 물리 DB의
    # 전체 사용자 데이터 테이블과 일치하므로, 순서만 맞으면 FK 위반 없이 지울 수 있다).
    ordered_tables = [t for t in target.metadata.sorted_tables if t.name in target.table_names]
    for table in reversed(ordered_tables):
        session.execute(delete(table))
    session.flush()

    for table in ordered_tables:
        rows = data.get(table.name) or []
        prepared = []
        for row in rows:
            prepared.append({col.name: _deserialize_value(row.get(col.name), col) for col in table.columns})
        if prepared:
            session.execute(insert(table), prepared)
        result_counts[table.name] = len(prepared)

    session.commit()

    if is_postgres:
        # [v4.6 마이그레이션 로직과 동일한 이유] INSERT로 PK를 직접 채워 넣으면 그
        # 테이블의 SERIAL 시퀀스는 갱신되지 않아, 복구 후 새로 추가하는 행이 이미 쓰인
        # id와 충돌할 수 있다 - 복구가 끝난 뒤 각 테이블의 시퀀스를 현재 최대값으로
        # 맞춰준다. session이 아니라 별도 커넥션을 새로 열어 처리한다(세션은 이미 커밋
        # 완료 상태).
        with engine.begin() as conn:
            for table in ordered_tables:
                _reset_pg_sequence(conn, table)

    return result_counts


def restore_backup_bundle(billing_db: Session, ops_db: Session, bundle: dict, confirm: bool) -> dict:
    """백업 번들을 Billing DB + Operations DB에 복구한다 (대상 테이블 전체 교체, 파괴적).

    confirm=False면 즉시 거부한다 - 라우터에서 요청 바디의 confirm 값을 그대로 넘긴다.
    """
    if not confirm:
        raise RestoreConfirmationError("confirm=true로 명시적으로 확인해야 복구가 실행됩니다")

    billing_counts = _restore_target(
        billing_db,
        billing_engine,
        _billing_backup_target(),
        bundle["billing"],
        is_postgres=billing_engine.dialect.name == "postgresql",
    )
    ops_counts = _restore_target(
        ops_db,
        ops_engine,
        _operations_backup_target(),
        bundle["operations"],
        is_postgres=ops_engine.dialect.name == "postgresql",
    )
    return {"billing": billing_counts, "operations": ops_counts}
