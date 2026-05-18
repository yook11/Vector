"""Article Acquisition Service — 1 source 分のニュースから品質を担保した記事を獲得する。

外部ニュースを取得して品質を担保した ``articles`` に到達させることが ``collection``
BC の業務目的。本 Service はその中核ユースケースで、source 1 件分の取り込みを担う。
即時獲得 (本文込みで品質を満たす) 経路と補完待ち獲得 (本文 HTML 取得を経て獲得確定
する) 経路の 2 系統を ``match`` で振り分けて永続化する。完成は後段
``ArticleCompletionService`` が担う。

新 2 表構成 (``articles`` / ``pending_html_articles``) を直接駆動する。

Fetcher の ``AsyncIterator[AnalyzableArticle | ObservedArticle]`` を回し
``match`` で分岐する:

- ``AnalyzableArticle`` → 即時獲得経路。
  ``article_store.save(ready)`` に passport 型を直接渡し、
  ``articles.source_url UNIQUE`` の ``ON CONFLICT DO NOTHING`` で同 tick race /
  既知 URL を吸収する (``None`` 戻りは静かに skip)。caller (``ingest_source`` task)
  は返却された ``article_id`` を ``ExtractionTrigger`` に詰めて
  ``extract_content.kiq`` に chain する。
- ``ObservedArticle`` → 補完待ち獲得経路。
  ``article_store.exists_by_source_url`` pre-check で feed 再露出を弾き、
  ``pending_html_articles.url`` で投入 (Stage 1 投入は ``PendingHtmlEnqueue``、
  Stage 2 の claim/sweep は ``article_completion/repository.py``)。下流は
  cron poller (``dispatch_html_fetch_jobs``) が DB 駆動で拾うため、Service /
  Task は pending_id を caller に渡さない (Outcome 純化原則)。

per-entry の品質ゲート未達は Fetcher 側で yield しない (Outcome 純化原則)。
Service は「渡される passport は次工程に進めるべきもの」という前提だけを持つ。

``commit`` までが Service の責務。``NewsSource`` ORM の lookup は
``IngestSourceArg`` (=task envelope) で済んでいるため本 Service では行わない。
成功側の監査焼付 (``pipeline_events.payload`` への件数 / breakdown 集計) は
中途半端な構造として撤去済。後続で proper な audit subsystem を再導入する予定。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.persistence.article_store import ArticleStore
from app.collection.source_fetch.errors import SourceFetchError
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.collection.source_fetch.protocol import Fetcher

logger = structlog.get_logger(__name__)


class ArticleAcquisitionService:
    """1 source 分のニュースを取り込み、品質を担保した記事を獲得する。

    即時獲得可能なものは ``articles`` に直接保存、本文補完を経て獲得するものは
    ``pending_html_articles`` に保管する (後段 ``ArticleCompletionService`` が
    完成させる)。

    ソース全体の取得失敗は ``SourceFetchError`` で呼び出し側 (Task) に伝播する。
    Stage 1 task は taskiq inline retry を持たず、監査して return → 次の cron tick
    で再 dispatch で救済する設計。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher_factory: Callable[[], Fetcher],
    ) -> None:
        self._session_factory = session_factory
        self._fetcher_factory = fetcher_factory

    async def execute(self, source_id: int) -> list[int]:
        async with self._session_factory() as session:
            fetcher = self._fetcher_factory()
            article_store = ArticleStore(session)
            pending_enqueue = PendingHtmlEnqueue(session)

            persisted_ids: list[int] = []

            try:
                async for item in fetcher.fetch(source_id):
                    match item:
                        case AnalyzableArticle() as ready:
                            article_id = await article_store.save(ready)
                            if article_id is None:
                                continue
                            persisted_ids.append(article_id)
                        case ObservedArticle() as observed:
                            # pre-check: 既知 URL の HTML fetch 反復を避けるための
                            # コスト節約 (UNIQUE(url) と ON CONFLICT は enqueue 側で
                            # 構造的に担保)
                            if await article_store.exists_by_source_url(
                                observed.source_url
                            ):
                                continue
                            await pending_enqueue.enqueue(
                                observed,
                                source_id=source_id,
                                ready_at=datetime.now(UTC),
                            )
            except ExternalFetchError as exc:
                # tool 層で origin error に翻訳済。Layer 1 marker に CODE ごと
                # 載せ替えて伝播する (cron 一本化のため Stage 1 は救済戦略の差を
                # 持たず、CODE は監査解像度のためだけに保持する)。
                raise SourceFetchError(str(exc), code=exc.CODE) from exc

            await session.commit()

        logger.info(
            "ingest_source_completed",
            source_id=source_id,
            persisted_count=len(persisted_ids),
        )
        return persisted_ids
