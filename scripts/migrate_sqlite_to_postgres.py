#!/usr/bin/env python
"""[v4.0] 기존 SQLite(data/billing.db) 데이터를 PostgreSQL로 옮기는 1회성 스크립트.

Docker/PostgreSQL 아키텍처로 전환하기 전, 이미 SQLite 기반으로 운영 중이던 배포본의
데이터를 새 PostgreSQL 컨테이너로 옮길 때 사용한다. 신규 설치(아직 SQLite에 데이터가
없는 경우)라면 이 스크립트를 실행할 필요 없이 그냥 docker compose로 새로 시작하면 된다.

동작 방식:
1. app/models.py의 Base.metadata에 정의된 모든 테이블을 외래키 의존성 순서대로
   (Base.metadata.sorted_tables) 순회한다 - 부모 테이블을 자식보다 먼저 적재해야
   FK 제약을 위반하지 않는다.
2. 각 테이블의 모든 행을 SQLite에서 읽어 그대로 PostgreSQL에 적재한다 (ORM을 거치지
   않고 Core 레벨 insert를 사용 - 컬럼 값을 그대로 복사하는 것이 목적이라 모델의
   default/validate 로직이 다시 실행될 필요가 없음).
3. 시퀀스(Postgres의 SERIAL/IDENTITY id 컬럼)가 SQLite에서 가져온 명시적 id 값과
   어긋나지 않도록, 각 테이블 적재 후 `setval`로 시퀀스를 현재 최대 id에 맞춰 재조정한다
   (이 단계를 건너뛰면 마이그레이션 이후 새로 INSERT되는 행이 이미 있는 id와 충돌할 수
   있다).

사용법 (docker compose 환경 기준):
    # 1) 기존 SQLite 파일을 이 저장소의 data/billing.db 위치에 둔다
    #    (예: 기존 서버의 /opt/vcf-billing-portal/data/billing.db를 scp로 복사)
    # 2) PostgreSQL 컨테이너만 먼저 띄운다
    docker compose up -d db
    # 3) 새 PostgreSQL이 비어있는 상태에서, api 이미지를 재사용해 이 스크립트를 실행
    docker compose run --rm -e DATABASE_URL="$(grep ^DATABASE_URL .env | cut -d= -f2-)" \\
        -v "$(pwd)/data:/app/data:ro" api python scripts/migrate_sqlite_to_postgres.py \\
        --sqlite-path /app/data/billing.db
    # 4) 검증 후 api/collector 컨테이너를 정상 기동
    docker compose up -d

주의: 대상 PostgreSQL DB는 반드시 비어 있어야 한다 (Base.metadata.create_all로 방금
생성된, 데이터가 없는 상태). 이미 데이터가 있는 PostgreSQL에 실행하면 PK 충돌로 실패한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, insert, select, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
import app.models  # noqa: E402,F401  (Base.metadata에 테이블을 등록시키기 위한 import)


def migrate(sqlite_path: str, postgres_url: str) -> None:
    sqlite_engine = create_engine(f"sqlite:///{sqlite_path}")
    postgres_engine = create_engine(postgres_url)

    Base.metadata.create_all(bind=postgres_engine)

    with sqlite_engine.connect() as src, postgres_engine.begin() as dst:
        for table in Base.metadata.sorted_tables:
            rows = src.execute(select(table)).mappings().all()
            if not rows:
                print(f"  - {table.name}: 0행 (건너뜀)")
                continue

            dst.execute(insert(table), [dict(row) for row in rows])
            print(f"  - {table.name}: {len(rows)}행 이관 완료")

            # id 컬럼(PK, autoincrement)이 있으면 시퀀스를 재조정한다.
            pk_cols = [c for c in table.primary_key.columns if c.autoincrement]
            for col in pk_cols:
                seq_name = f"{table.name}_{col.name}_seq"
                dst.execute(
                    text(
                        f"SELECT setval('{seq_name}', COALESCE((SELECT MAX({col.name}) FROM {table.name}), 1), true)"
                    )
                )

    print("마이그레이션 완료.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SQLite -> PostgreSQL 1회성 데이터 마이그레이션")
    parser.add_argument(
        "--sqlite-path",
        default=str(Path(__file__).resolve().parent.parent / "data" / "billing.db"),
        help="원본 SQLite 파일 경로 (기본: ./data/billing.db)",
    )
    parser.add_argument(
        "--postgres-url",
        default=None,
        help="대상 PostgreSQL URL (기본: .env의 DATABASE_URL 설정값 사용)",
    )
    args = parser.parse_args()

    if not Path(args.sqlite_path).exists():
        print(f"오류: SQLite 파일을 찾을 수 없습니다: {args.sqlite_path}", file=sys.stderr)
        sys.exit(1)

    postgres_url = args.postgres_url or get_settings().database_url
    if not postgres_url.startswith("postgresql"):
        print(
            f"오류: 대상 URL이 PostgreSQL이 아닙니다 ({postgres_url}). "
            "--postgres-url로 명시하거나 DATABASE_URL을 postgresql+psycopg://...로 설정하세요.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"SQLite({args.sqlite_path}) -> PostgreSQL 마이그레이션 시작")
    migrate(args.sqlite_path, postgres_url)
