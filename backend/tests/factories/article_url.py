"""テスト用 ``ArticleUrl`` 共通ファクトリ。

article 行は ``article_url_id`` を必ず持つ前提のため、テストフィクスチャは
本ヘルパー経由で最小限の ``ArticleUrl`` を 1 件 INSERT する形に統一する。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_url import ArticleUrl
from app.models.news_source import NewsSource


async def create_article_url(
    session: AsyncSession,
    *,
    source: NewsSource,
    url: str,
) -> ArticleUrl:
    """テスト用に ``ArticleUrl`` を 1 件 INSERT して返す。

    ``normalized_url`` / ``original_url`` を同一値とし、
    ``first_seen_source_id`` に渡した ``source.id`` を入れる最小フィクスチャ。
    呼び出し側は返り値の ``id`` を ``Article(article_url_id=...)`` に渡す。
    """
    article_url = ArticleUrl(
        normalized_url=url,
        original_url=url,
        first_seen_source_id=source.id,
    )
    session.add(article_url)
    await session.flush()
    return article_url
