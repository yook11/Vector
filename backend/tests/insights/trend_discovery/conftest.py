"""Trend Discovery BC テスト固有のフィクスチャ。

repository / Service テスト向けの ``seed_analysis`` ファクトリを提供する。
seed_analysis は 1 件の ``AnalyzedArticleRecord`` を関連 ORM (AnalyzableArticleRecord /
ArticleCuration) とともに作成し、``AnalyzedArticleRecord.key_points`` JSONB に
mention 列を焼き付ける。

PR 2 で集計軸を ``article_extraction_entities`` から
``analyzed_articles.key_points`` JSONB の mention に切替したため、本 fixture も
mention 軸に書き直されている。

URL の重複制約を避けるため fixture 内のカウンタで一意な URL を採番する
(関数スコープ fixture なのでテストごとにリセットされる)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from itertools import count

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.news_source import NewsSource

SeedAnalysis = Callable[..., Awaitable[AnalyzedArticleRecord]]

# AnalyzedArticleRecord.embedding は HALFVEC(768)。テストは近接 dedup を作り込みやすい
# よう短いベクトルを渡し、ここで 768 次元へ 0 padding する。
_EMBEDDING_DIM = 768


def _pad_embedding(embedding: Sequence[float] | None) -> list[float] | None:
    if embedding is None:
        return None
    vec = list(embedding)
    return (vec + [0.0] * _EMBEDDING_DIM)[:_EMBEDDING_DIM]


@pytest.fixture
def seed_analysis(db_session: AsyncSession, sample_source: NewsSource) -> SeedAnalysis:
    """1 件の ``AnalyzedArticleRecord`` を関連 ORM ごと seed するファクトリ。

    Args (キーワード引数):
        category_id: ``AnalyzedArticleRecord.category_id`` に設定する FK。
        analyzed_at: ``analyzed_at`` を明示指定 (server_default を上書き)。
        mentions: ``[(surface, type), ...]`` の列。``key_points`` JSONB に
            1 つの key_point としてまとめて焼き付ける (同一 assessment 内で同じ
            mention が複数 key_point に現れても COUNT(DISTINCT a.id) で 1 件と
            数えられる集計仕様を踏襲)。
        content: 単一 key_point seeding (``mentions`` 経由) の content を上書き。
        key_points: ``[(content, [(surface, type), ...]), ...]`` で複数 key_point を
            明示 seeding する (指定時は ``mentions`` / ``content`` より優先)。同一
            assessment 内の別 key_point 共起や記事内 dedup の検証に使う。
        embedding: ``AnalyzedArticleRecord.embedding`` に焼く float 列。768 次元未満は
            0 padding する (近接 dedup 検証用に短いベクトルを渡せる)。None は
            embedding 未設定 (旧行) を再現する。
        key_points_null: ``True`` のとき ``key_points`` を NULL のまま残す
            (PR 1 デプロイ前の旧行を再現する用途)。

    Returns:
        永続化済みの ``AnalyzedArticleRecord``。flush のみで commit はしない
        (呼び出し側のトランザクション境界に従う)。
    """
    seq = count()

    async def _seed(
        *,
        category_id: int,
        analyzed_at: datetime,
        mentions: Sequence[tuple[str, str]] = (),
        content: str | None = None,
        key_points: Sequence[tuple[str, Sequence[tuple[str, str]]]] | None = None,
        embedding: Sequence[float] | None = None,
        key_points_null: bool = False,
    ) -> AnalyzedArticleRecord:
        n = next(seq)
        url = f"https://example.com/seed-{n}"

        article = AnalyzableArticleRecord(
            source_id=sample_source.id,
            source_url=url,
            original_title=f"seed-{n}",
            original_content="x" * 60,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(article)
        await db_session.flush()

        extraction = ArticleCuration(
            analyzable_article_id=article.id,
            translated_title=f"seed-{n}",
            summary="summary body",
        )
        db_session.add(extraction)
        await db_session.flush()

        key_points_json: list[dict[str, object]] | None
        if key_points_null:
            key_points_json = None
        elif key_points is not None:
            key_points_json = [
                {
                    "content": kp_content,
                    "mentions": [
                        {"surface": surface, "type": type_}
                        for surface, type_ in kp_mentions
                    ],
                }
                for kp_content, kp_mentions in key_points
            ]
        elif mentions:
            key_points_json = [
                {
                    "content": content or f"seed key point {n}",
                    "mentions": [
                        {"surface": surface, "type": type_}
                        for surface, type_ in mentions
                    ],
                }
            ]
        else:
            key_points_json = []

        analysis = AnalyzedArticleRecord(
            curation_id=extraction.id,
            translated_title=f"seed-{n}",
            summary="summary body",
            investor_take="investor take body",
            category_id=category_id,
            analyzed_at=analyzed_at,
            key_points=key_points_json,
            embedding=_pad_embedding(embedding),
        )
        db_session.add(analysis)
        await db_session.flush()
        return analysis

    return _seed
