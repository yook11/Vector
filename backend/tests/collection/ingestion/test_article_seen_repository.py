"""``ArticleSeenRepository`` (Pattern H pre-check) の統合テスト。

ingestion BC が ``IncompleteArticle`` を pending 化する前の軽量存在確認を
検証する。既存 article の作成は ``ArticleRepository.save`` (両 BC 共通の
articles CRUD primitive) を経由する — Repository 役割が分かれているのは
正常で、`save` を呼んだ後に `exists_by_source_url` で読み戻す前処理は
本テストの想定する利用形態。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.repository import ArticleRepository
from app.collection.ingestion.article_seen_repository import ArticleSeenRepository
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


def _draft(body: str = "x" * 60) -> ArticleDraft:
    return ArticleDraft(title="Title", body=body, published_at=None)


@pytest.mark.asyncio
async def test_exists_by_source_url_returns_true_when_present(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    canonical = SafeUrl("https://example.com/article/seen-true")
    await ArticleRepository(db_session).save(
        _draft(), source_id=sample_source.id, source_url=canonical
    )
    await db_session.commit()

    repo = ArticleSeenRepository(db_session)
    assert await repo.exists_by_source_url(canonical) is True


@pytest.mark.asyncio
async def test_exists_by_source_url_returns_false_when_absent(
    db_session: AsyncSession,
) -> None:
    assert (
        await ArticleSeenRepository(db_session).exists_by_source_url(
            SafeUrl("https://example.com/article/seen-false")
        )
        is False
    )
