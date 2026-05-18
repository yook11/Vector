"""複数 feed を持つ source (NASA / Cornell Chronicle) の per-feed fan-out 共通処理。

per-feed 巡回 + feed 横断 URL dedup + per-feed 失敗隔離を行う。1 feed の失敗で
source 全体は落とさず、全 feed 失敗時のみ最初の error を re-raise する。dedup の
正しさは DB ``articles.source_url UNIQUE`` + ``ON CONFLICT`` が所有し、
``seen_urls`` は同 cron 周期内の no-op INSERT を省くコスト最適化に過ぎない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import structlog

from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.source_fetch.tools.rss_parser import ParseMode, RssEntry

logger = structlog.get_logger(__name__)


def _no_body(_entry: RssEntry) -> str | None:
    """既定の body builder: 本文を持たない (body=None)。"""
    return None


async def multi_feed_rss(
    tools: FetchTools,
    *,
    source_name: str,
    feeds: tuple[str, ...],
    parse_mode: ParseMode = "text",
    body_builder: Callable[[RssEntry], str | None] = _no_body,
) -> AsyncIterator[FetchedArticle]:
    """``feeds`` を per-feed 巡回し ``FetchedArticle`` を yield する。

    ``parse_mode`` 既定 ``"text"`` (Shift_JIS など bytes sniff が要る feed は
    ``"bytes"``)。``body_builder`` 既定は body なし。
    """
    seen_urls: set[str] = set()
    success_count = 0
    first_error: ExternalFetchError | None = None

    for feed_url in feeds:
        try:
            entries = await tools.rss.fetch(
                endpoint_url=feed_url,
                source_name=source_name,
                parse_mode=parse_mode,
            )
        except ExternalFetchError as exc:
            # この feed をログに記録して次 feed へ。source 全体は落とさない。
            logger.warning(
                "source_feed_fetch_failed",
                source=source_name,
                feed=feed_url,
                code=exc.CODE,
                error=str(exc),
            )
            if first_error is None:
                first_error = exc  # surface 用に最初のみ保持
            continue

        # fetch() が return した時点で feed 成功 (entries 空でも成功)。
        success_count += 1
        logger.info(
            "source_feed_fetched",
            source=source_name,
            feed=feed_url,
            entries_count=len(entries),
        )
        for entry in entries:
            if not entry.link or entry.link in seen_urls:
                continue
            seen_urls.add(entry.link)
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=body_builder(entry),
                published_at=entry.published,
            )

    # 全 feed 失敗のときだけ source failure として surface する
    # (≥1 feed 成功なら正常終了)。
    if success_count == 0 and first_error is not None:
        raise first_error
