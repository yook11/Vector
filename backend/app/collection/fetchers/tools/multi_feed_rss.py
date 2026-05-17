"""複数 feed を持つ source 用の per-feed fan-out 共通処理 (P2-D)。

「1 source = 多数 feed」構造を取る source (NASA / Cornell Chronicle) の取得
共通処理。per-feed 巡回 + feed 横断 URL dedup + per-feed 失敗隔離を 1 箇所に
集約する。

P1 まで: 継承基底で subclass が ``FEEDS`` ClassVar (+ Pattern R なら本文
override) を差し替え。
P2(B+C): ``MultiFeedRssAdapter`` 汎用 machinery クラス (config を ``__init__``
注入)。
P2-D (本実装): Adapter 概念除去。本モジュールは **free function**
``multi_feed_rss(tools, *, source_name, feeds, parse_mode, body_builder)``
として共通処理だけを持つ。Source 定義 (``NASA_FEEDS`` / ``nasa_build_body`` /
identity / 補完方針) は ``NASASource`` / ``CornellChronicleSource``
(``nasa.py`` / ``cornell.py``) が宣言し、その ``collect`` が本関数へ委譲する。

per-feed 失敗隔離 (本共通処理の核):

- 1 feed の ``ExternalFetchError`` は **種類問わず** (recoverable /
  404=``FetchResourceNotFoundError`` / bozo=``FetchParseError`` /
  SSRF=``FetchSsrfBlockedError`` 等) ``source_feed_fetch_failed`` warning に
  記録して次 feed へ進む。1 feed の失敗で source 全体を落とさない
  (Stage 1 では feed レベルでも Permanent/Temporary 区別に業務的意味が
  無い — spec 原則 8 の feed レベルへの延長)。
- ``tools.rss.fetch`` が ``list[RssEntry]`` を return した時点でその feed は
  成功 (entries 0 件でも成功)。``source_feed_fetched`` info に件数を残す。
- **全 feed が失敗したときだけ** 最初の ``ExternalFetchError`` を re-raise
  する。これは generator body level (loop / except / finally の外) で起き、
  consumer chain (``service.execute`` の ``async for`` ← ``ArticleFetcher.fetch``
  ← ``collect()`` ← 本関数) を素通りして ``service.execute`` の
  ``except ExternalFetchError → SourceFetchError(code=exc.CODE)`` に合流する。
  ≥1 feed 成功なら正常終了 (FetchLog SUCCESS、partial は per-feed ログから
  導出する派生ビューであり状態に昇格させない)。

GeneratorExit 安全性:

- ``try`` は ``await tools.rss.fetch(...)`` のみを包む。entry 反復 +
  ``yield`` は catch 外なので consumer 例外 / ``GeneratorExit`` を呑まない。
- re-raise は **最初の** error (feed 順で決定的)。``pipeline_events.code`` は
  単一 column のため複数 code の全容は per-feed ログ側が保持する。
- ``XxxSource.collect`` は本 async generator を ``async def``+``yield`` で
  包まず plain ``@classmethod`` が ``return multi_feed_rss(...)`` で forward
  する (余分な generator frame を挟まず上記意味論を byte 保存する)。

dedup の正しさは DB ``articles.source_url UNIQUE`` + ``ON CONFLICT`` が所有
する。``seen_urls`` は同 cron 周期内の no-op INSERT を省くコスト最適化に
過ぎない (memory: ``feedback_structural_guarantee``)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import structlog

from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import ParseMode, RssEntry

logger = structlog.get_logger(__name__)


def _no_body(_entry: RssEntry) -> str | None:
    """Pattern H 既定の body builder: 本文は HTML 詳細ページに委譲 (body=None)。"""
    return None


async def multi_feed_rss(
    tools: FetchTools,
    *,
    source_name: str,
    feeds: tuple[str, ...],
    parse_mode: ParseMode = "text",
    body_builder: Callable[[RssEntry], str | None] = _no_body,
) -> AsyncIterator[FetchedArticle]:
    """``feeds`` を per-feed 巡回し ``FetchedArticle`` を yield する共通処理。

    ``source_name`` / ``feeds`` / ``parse_mode`` / ``body_builder`` は Source
    定義 (``NASASource`` / ``CornellChronicleSource``) が宣言する。``parse_mode``
    既定 ``"text"`` (Shift_JIS など bytes sniff が要る feed は ``"bytes"``)。
    ``body_builder`` 既定は Pattern H (body なし)、Pattern R (NASA) は
    ``content_encoded`` から本文を組む callable を渡す。
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
            # 種類問わず (recoverable / 404 / bozo / SSRF …) この feed を
            # 構造化ログに記録して次 feed へ。source 全体失敗にしない。
            logger.warning(
                "source_feed_fetch_failed",
                source=source_name,
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

    # 全 feed 失敗のときだけ source failure として surface する。
    # ≥1 feed 成功なら正常終了 (FetchLog SUCCESS、partial は per-feed
    # ログで導出)。空 feeds 防御として is not None も残す (型 narrowing)。
    if success_count == 0 and first_error is not None:
        raise first_error
