"""digest BC テスト固有のフィクスチャ。

repository / Service テスト向けの ``seed_analysis`` ファクトリを提供する。
seed_analysis は 1 件の ``InScopeAssessment`` を関連 ORM (Article /
ArticleCuration) とともに作成し、``InScopeAssessment.events`` JSONB に
mention 列を焼き付ける。

PR 2 で集計軸を ``article_extraction_entities`` から
``in_scope_assessments.events`` JSONB の mention に切替したため、本 fixture も
mention 軸に書き直されている。

URL の重複制約を避けるため fixture 内のカウンタで一意な URL を採番する
(関数スコープ fixture なのでテストごとにリセットされる)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from itertools import count

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource

SeedAnalysis = Callable[..., Awaitable[InScopeAssessment]]


@pytest.fixture
def seed_analysis(db_session: AsyncSession, sample_source: NewsSource) -> SeedAnalysis:
    """1 件の ``InScopeAssessment`` を関連 ORM ごと seed するファクトリ。

    Args (キーワード引数):
        category_id: ``InScopeAssessment.category_id`` に設定する FK。
        analyzed_at: ``analyzed_at`` を明示指定 (server_default を上書き)。
        mentions: ``[(surface, type), ...]`` の列。``events`` JSONB に
            1 つの event としてまとめて焼き付ける (同一 assessment 内で同じ
            mention が複数 event に現れても COUNT(DISTINCT a.id) で 1 件と
            数えられる集計仕様を踏襲)。
        events_null: ``True`` のとき ``events`` を NULL のまま残す
            (PR 1 デプロイ前の旧行を再現する用途)。

    Returns:
        永続化済みの ``InScopeAssessment``。flush のみで commit はしない
        (呼び出し側のトランザクション境界に従う)。
    """
    seq = count()

    async def _seed(
        *,
        category_id: int,
        analyzed_at: datetime,
        mentions: Sequence[tuple[str, str]] = (),
        events_null: bool = False,
    ) -> InScopeAssessment:
        n = next(seq)
        url = f"https://example.com/seed-{n}"

        article = Article(
            source_id=sample_source.id,
            source_url=url,
            original_title=f"seed-{n}",
            original_content="x" * 60,
        )
        db_session.add(article)
        await db_session.flush()

        extraction = ArticleCuration(
            article_id=article.id,
            translated_title=f"seed-{n}",
            summary="summary body",
        )
        db_session.add(extraction)
        await db_session.flush()

        if events_null:
            events: list[dict[str, object]] | None = None
        elif mentions:
            events = [
                {
                    "description": f"seed event {n}",
                    "mentions": [
                        {"surface": surface, "type": type_}
                        for surface, type_ in mentions
                    ],
                }
            ]
        else:
            events = []

        analysis = InScopeAssessment(
            curation_id=extraction.id,
            translated_title=f"seed-{n}",
            summary="summary body",
            investor_take="investor take body",
            category_id=category_id,
            analyzed_at=analyzed_at,
            events=events,
        )
        db_session.add(analysis)
        await db_session.flush()
        return analysis

    return _seed
