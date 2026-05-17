"""複数 feed を持つ source の fan-out 共通基底 (NASA / Cornell)。

``FEEDS`` ClassVar を持つ source は「1 source = 多数 feed」構造を取る。
本基底は per-feed 巡回 + feed 横断 URL dedup + per-feed 失敗隔離を 1 箇所に
集約する。subclass は ClassVar 宣言 (+ Pattern R なら ``_build_body``
override) だけの thin subclass になる (``BaseDjangoplicityAdapter`` /
``BaseMDPICrossrefAdapter`` と同形)。

per-feed 失敗隔離 (本基底の核):

- 1 feed の ``ExternalFetchError`` は **種類問わず** (recoverable /
  404=``FetchResourceNotFoundError`` / bozo=``FetchParseError`` /
  SSRF=``FetchSsrfBlockedError`` 等) ``source_feed_fetch_failed`` warning に
  記録して次 feed へ進む。1 feed の失敗で source 全体を落とさない
  (Stage 1 では feed レベルでも Permanent/Temporary 区別に業務的意味が
  無い — spec 原則 8 の feed レベルへの延長)。
- ``RssParser.fetch`` が ``list[RssEntry]`` を return した時点でその feed は
  成功 (entries 0 件でも成功)。``source_feed_fetched`` info に件数を残す。
- **全 feed が失敗したときだけ** 最初の ``ExternalFetchError`` を re-raise
  する。これは generator body level (loop / except / finally の外) で起き、
  consumer chain (``service.execute`` の ``async for`` ← ``ArticleFetcher.fetch``
  ← ``collect()``) を素通りして ``service.execute`` の
  ``except ExternalFetchError → SourceFetchError(code=exc.CODE)`` に合流する。
  ≥1 feed 成功なら正常終了 (FetchLog SUCCESS、partial は per-feed ログから
  導出する派生ビューであり状態に昇格させない)。

GeneratorExit 安全性:

- ``try`` は ``await self._parser.fetch(...)`` のみを包む。entry 反復 +
  ``yield`` は catch 外なので consumer 例外 / ``GeneratorExit`` を呑まない。
- re-raise は **最初の** error (feed 順で決定的)。``pipeline_events.code`` は
  単一 column のため複数 code の全容は per-feed ログ側が保持する。

dedup の正しさは DB ``articles.source_url UNIQUE`` + ``ON CONFLICT`` が所有
する。``seen_urls`` は同 cron 周期内の no-op INSERT を省くコスト最適化に
過ぎない (memory: ``feedback_structural_guarantee``)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

import structlog

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import ParseMode, RssEntry, RssParser

logger = structlog.get_logger(__name__)


class BaseMultiFeedRssAdapter:
    """``FEEDS`` を持つ source の per-feed fan-out SourceAdapter 共通基底。

    subclass は ``NAME`` / ``ENDPOINT_URL`` / ``FEEDS`` ClassVar を必須で
    差し替える。``PARSE_MODE`` は既定 ``"text"`` (Shift_JIS など bytes sniff
    が要る feed は ``"bytes"`` を宣言)。Pattern R (本文 RSS 直取り) の
    subclass は ``_build_body`` を override する (既定は Pattern H = body
    なし)。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    FEEDS: ClassVar[tuple[str, ...]]
    PARSE_MODE: ClassVar[ParseMode] = "text"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    def _build_body(self, entry: RssEntry) -> str | None:
        """Pattern H 既定: 本文は HTML 詳細ページに委譲 (body=None)。

        Pattern R の subclass (NASA) は ``content_encoded`` から本文を組む
        override を持つ。
        """
        return None

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        seen_urls: set[str] = set()
        success_count = 0
        first_error: ExternalFetchError | None = None

        for feed_url in self.FEEDS:
            try:
                entries = await self._parser.fetch(
                    endpoint_url=feed_url,
                    source_name=self.NAME,
                    parse_mode=self.PARSE_MODE,
                )
            except ExternalFetchError as exc:
                # 種類問わず (recoverable / 404 / bozo / SSRF …) この feed を
                # 構造化ログに記録して次 feed へ。source 全体失敗にしない。
                logger.warning(
                    "source_feed_fetch_failed",
                    source=self.NAME,
                    feed=feed_url,
                    code=exc.CODE,
                    error=str(exc),
                )
                if first_error is None:
                    first_error = exc  # source surface 用に最初のみ保持
                continue

            # fetch() が return した時点で feed 成功 (entries 空でも成功)。
            # rss_parser は list を返すので以降の反復は例外を出さない。
            success_count += 1
            logger.info(
                "source_feed_fetched",
                source=self.NAME,
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
                    body=self._build_body(entry),
                    published_at=entry.published,
                )

        # 全 feed 失敗のときだけ source failure として surface する。
        # ≥1 feed 成功なら正常終了 (FetchLog SUCCESS、partial は per-feed
        # ログで導出)。空 FEEDS 防御として is not None も残す (型 narrowing)。
        if success_count == 0 and first_error is not None:
            raise first_error
