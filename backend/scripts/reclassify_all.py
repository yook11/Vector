"""全 article_extractions に対して Stage 2 (分類) を再実行する。

完全リセット（rev_G）の直後に 1 度だけ実行する想定。classify_content タスクを
broker_analysis に投入し、ワーカーが順次処理する。Stage 3 (embedding) は
classify_content の中でチェーンされるため、結果として埋め込みも再生成される。

Usage:
    docker compose exec backend python scripts/reclassify_all.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.tasks import classify_content
from app.brokers import broker_analysis
from app.db import engine
from app.models.article_extraction import ArticleExtraction


async def main() -> None:
    async with AsyncSession(engine) as session:
        result = await session.execute(select(ArticleExtraction.article_id))
        article_ids = [row[0] for row in result]

    print(f"enqueueing {len(article_ids)} reclassification tasks")

    await broker_analysis.startup()
    try:
        for aid in article_ids:
            await classify_content.kiq(aid)
    finally:
        await broker_analysis.shutdown()

    print("done")


if __name__ == "__main__":
    asyncio.run(main())
