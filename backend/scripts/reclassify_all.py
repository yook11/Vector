"""全 article_extractions に対して Stage 4 (Assessment) を再実行する。

完全リセット（rev_G）の直後に 1 度だけ実行する想定。assess_content タスクを
broker_analysis に投入し、ワーカーが順次処理する。Stage 5 (embedding) は
assess_content の中でチェーンされるため、結果として埋め込みも再生成される。

Pattern A' (spec §3.4 / §7.2) maintenance script として、自身が gatekeeper を
兼ねる: 各 extraction に対して `ReadyForAssessment.try_advance_from` を呼び、
成立するもののみ enqueue する (既に評価済みは自然に skip)。

Usage:
    docker compose exec backend python scripts/reclassify_all.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.out_of_scope_repository import OutOfScopeRepository
from app.analysis.assessment.repository import InScopeRepository
from app.analysis.assessment.tasks import assess_content
from app.analysis.extraction.repository import ExtractionRepository
from app.brokers import broker_analysis
from app.db import engine
from app.models.article_extraction import ArticleExtraction


async def main() -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with AsyncSession(engine) as session:
        result = await session.execute(
            select(ArticleExtraction.article_id, ArticleExtraction.id)
        )
        rows = [(row[0], row[1]) for row in result]

    print(f"evaluating {len(rows)} extractions for reassessment")

    await broker_analysis.startup()
    enqueued = 0
    skipped = 0
    try:
        for article_id, _extraction_id in rows:
            async with session_factory() as session:
                extraction_repo = ExtractionRepository(session)
                in_scope_repo = InScopeRepository(session)
                out_of_scope_repo = OutOfScopeRepository(session)
                extraction = await extraction_repo.find_by_article_id(article_id)
                if extraction is None:
                    skipped += 1
                    continue
                ready = await ReadyForAssessment.try_advance_from(
                    extraction,
                    in_scope_repo=in_scope_repo,
                    out_of_scope_repo=out_of_scope_repo,
                )
            if ready is None:
                skipped += 1
                continue
            await assess_content.kiq(ready)
            enqueued += 1
    finally:
        await broker_analysis.shutdown()

    print(f"done: enqueued={enqueued} skipped={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
