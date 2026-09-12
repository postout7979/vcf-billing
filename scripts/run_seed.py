#!/usr/bin/env python
"""편의 진입점: `python scripts/run_seed.py --days 14 --reset`"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.seed_data import seed  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VCF Billing Portal 샘플 데이터 시딩")
    parser.add_argument("--days", type=int, default=14, help="백필할 과거 일수 (기본 14일)")
    parser.add_argument("--reset", action="store_true", help="기존 DB를 초기화하고 새로 생성")
    args = parser.parse_args()
    seed(days=args.days, reset=args.reset)
