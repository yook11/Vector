"""記事永続化 — フェッチャーが生成した ArticleCandidate を DB に保存する。

全フェッチャー（RSS / HN / AV）で共通の永続化ロジック:
URL 重複排除、max_articles 制限、session.add を一箇所に集約する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.domain.safe_url import SafeUrl
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)

_TITLE_MAX_LENGTH = 500


@dataclass(frozen=True)
class ArticleCandidate:
    """フェッチャーが生成する記事の中間表現。

    外部配信形式 (RSS / HN API 等) から ingestion 境界を越える際の正規化済みデータ。
    生文字列からの構築は ``from_external`` 経由で行い、URL 安全性と
    タイトル整形 (HTML 除去・長さ上限) を構造的に保証する。
    """

    url: SafeUrl
    title: str

    @classmethod
    def from_external(cls, *, raw_url: str, raw_title: str) -> ArticleCandidate | None:
        """外部ソースの生文字列から候補を構築する。

        正規化に失敗する（不正 URL / 空タイトル）場合は ``None`` を返し、
        呼び出し側でエントリをスキップする運用を想定する。
        """
        if not raw_url:
            return None
        try:
            safe_url = SafeUrl(raw_url)
        except (ValueError, ValidationError):
            return None

        clean_title = (strip_html_tags(raw_title) or "")[:_TITLE_MAX_LENGTH]
        if not clean_title:
            return None

        return cls(url=safe_url, title=clean_title)


@dataclass
class PersistResult:
    """``persist_new_articles`` の内部結果 — 実際に新規追加された DiscoveredArticle。"""

    new_discovered: list[DiscoveredArticle] = field(default_factory=list)


async def persist_new_articles(
    session: AsyncSession,
    source: NewsSource,
    candidates: dict[SafeUrl, ArticleCandidate],
) -> PersistResult:
    """ArticleCandidate を DB に保存する。

    入力 dict のキー一意性により URL 重複は型レベルで排除されている。
    本関数は DB 既存 URL との突き合わせのみを行う。

    Args:
        session: DB セッション（コミットは呼び出し側の責任）。
        source: 記事の取得元ソース。
        candidates: 呼び出し側で URL 重複排除済みの候補 dict。

    Returns:
        新規発見記事を含む PersistResult。
    """
    result = PersistResult()

    if not candidates:
        return result

    # DB 既存 URL を確認（existing_urls は「DB 既存」のみを意味する）
    urls = list(candidates.keys())
    existing_urls: set[SafeUrl] = set()
    chunk_size = 500
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        stmt = select(DiscoveredArticle.original_url).where(
            DiscoveredArticle.original_url.in_(chunk)
        )
        rows = await session.execute(stmt)
        existing_urls.update(row[0] for row in rows.all())

    # 新規 discovered_articles を作成
    max_new = settings.max_articles_per_fetch

    for url, candidate in candidates.items():
        if url in existing_urls:
            continue

        if len(result.new_discovered) >= max_new:
            logger.info("source_fetch_limit_reached", source=source.name, max=max_new)
            break

        discovered = DiscoveredArticle(
            original_title=candidate.title,
            original_url=url,
            news_source_id=source.id,
        )

        session.add(discovered)
        result.new_discovered.append(discovered)

    return result
