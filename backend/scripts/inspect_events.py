"""dev 用: 最新 N 件の assessments の events を目視確認するスクリプト。

event-extraction PR 1 デプロイ後、自然流入の検証データを確認する用途。
in_scope_assessments / out_of_scope_assessments の両テーブルから events
カラムが NULL でない (= PR 1 以降に生成された) 行を新着順に表示する。

Usage:
    docker compose exec backend python scripts/inspect_events.py --limit 50
    docker compose exec backend python scripts/inspect_events.py \
        --limit 30 --kind in_scope
    docker compose exec backend python scripts/inspect_events.py \
        --kind out_of_scope
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.db import engine
from app.models.in_scope_assessment import InScopeAssessment
from app.models.out_of_scope_assessment import OutOfScopeAssessment


async def _print_in_scope(session: AsyncSession, limit: int) -> None:
    stmt = (
        select(InScopeAssessment)
        .where(InScopeAssessment.events.is_not(None))
        .order_by(InScopeAssessment.analyzed_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    print(f"=== in_scope_assessments (latest {len(rows)}) ===")
    for row in rows:
        print(f"--- id={row.id} extraction_id={row.extraction_id} ---")
        print(f"title: {row.translated_title}")
        print(f"category_id={row.category_id} topic={row.topic!r}")
        print(f"investor_take: {row.investor_take[:120]}")
        events = row.events or []
        print(f"events ({len(events)}):")
        print(json.dumps(events, ensure_ascii=False, indent=2))
        print()


async def _print_out_of_scope(session: AsyncSession, limit: int) -> None:
    stmt = (
        select(OutOfScopeAssessment)
        .where(OutOfScopeAssessment.events.is_not(None))
        .order_by(OutOfScopeAssessment.rejected_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    print(f"=== out_of_scope_assessments (latest {len(rows)}) ===")
    for row in rows:
        print(f"--- id={row.id} extraction_id={row.extraction_id} ---")
        print(f"title: {row.translated_title}")
        print(f"investor_take: {row.investor_take[:120]}")
        events = row.events or []
        print(f"events ({len(events)}):")
        print(json.dumps(events, ensure_ascii=False, indent=2))
        print()


async def main(limit: int, kind: str) -> None:
    async with AsyncSession(engine) as session:
        if kind in ("in_scope", "both"):
            await _print_in_scope(session, limit)
        if kind in ("out_of_scope", "both"):
            await _print_out_of_scope(session, limit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--kind",
        choices=["in_scope", "out_of_scope", "both"],
        default="both",
        help="表示対象テーブル (default: both)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.kind))
