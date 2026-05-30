"""複数 feed を持つ source (NASA / Cornell Chronicle) の per-feed fan-out 道具。

per-feed 巡回 + per-feed 失敗隔離を担う取得 I/O の頑健性層。1 feed の失敗で
source 全体は落とさず、全 feed 失敗時のみ最初の error を re-raise する。結合した
``RssEntry`` 列を返すだけで、**dedup も写像も持たない** (横断 dedup は Source の
``select``、``FetchedArticle`` への写像は Source の ``map_entry`` が担う)。
"""

from __future__ import annotations

import structlog

from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.article_acquisition.reader.rss_reader import (
    ParseMode,
    RssEntry,
    RssReader,
)
from app.collection.external_fetch_errors import ExternalFetchError

logger = structlog.get_logger(__name__)


class MultiFeedRssReader:
    """共有 ``RssReader`` を per-feed に駆動する fan-out reader。

    transport は注入された ``rss`` を再利用する (fixture が同 1 reader を差し替え
    できるよう field でなく ``ReaderTools.multi_feed_rss()`` factory 経由で wrap)。
    """

    def __init__(self, *, rss: RssReader) -> None:
        self._rss = rss

    async def fetch(
        self,
        *,
        source_name: str,
        feeds: tuple[str, ...],
        parse_mode: ParseMode = "text",
    ) -> list[RssEntry]:
        """``feeds`` を per-feed 巡回し結合 ``RssEntry`` 列を返す。

        per-feed の read error (接続失敗 ``ExternalFetchError`` / 読取失敗
        ``UnreadableResponseError``) はログに記録して次 feed へ。全 feed が失敗した
        ときだけ最初の error を re-raise する (≥1 feed 成功なら正常終了、entries 空
        でも成功扱い)。
        """
        collected: list[RssEntry] = []
        success_count = 0
        first_error: ExternalFetchError | UnreadableResponseError | None = None

        for feed_url in feeds:
            try:
                entries = await self._rss.fetch(
                    endpoint_url=feed_url,
                    source_name=source_name,
                    parse_mode=parse_mode,
                )
            except (ExternalFetchError, UnreadableResponseError) as exc:
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
            collected.extend(entries)

        # 全 feed 失敗のときだけ source failure として surface する
        # (≥1 feed 成功なら正常終了)。
        if success_count == 0 and first_error is not None:
            raise first_error

        return collected
