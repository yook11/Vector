"""Article Acquisition Service — 1 source 分のニュースから記事を獲得する。

Fetcher の ``AsyncIterator`` を回し ``match`` で 3 型を振り分ける:

- ``AnalyzableArticle`` → ``article_store.save`` で即時獲得 (``source_url``
  UNIQUE の ON CONFLICT で同 tick race / 既知 URL を吸収、``None`` は skip)。
- ``ObservedArticle`` → ``exists_by_source_url`` pre-check 後
  ``pending_html_articles`` に投入 (補完は後段 ``ArticleCompletionService``)。
- ``ConversionRejection`` → 変換不能 entry。**業務 tx とは別 session** で
  棄却監査を焼いて continue (後続の source 全体失敗で業務 session が rollback
  しても監査を残すため。``failure_handling.py`` の 3 段防御 doctrine 再利用)。

``commit`` までが責務。``NewsSource`` lookup は ``IngestSourceArg`` 済で本
Service では行わない。成功側の件数監査は撤去済 (後続で再導入予定)。
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
from app.collection.source_fetch.audit_repository import SourceFetchAuditRepository
from app.collection.source_fetch.errors import SourceFetchError
from app.collection.source_fetch.fetched_article_converter import ConversionRejection
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.collection.source_fetch.protocol import Fetcher
from app.observability.redact import redact_secrets

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
                        case ConversionRejection() as rej:
                            # 変換不能 entry。stream は止めず別 tx で監査して
                            # 次 entry へ (本ループの業務 session には書かない)。
                            await self._audit_conversion_rejected(source_id, rej)
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

    async def _audit_conversion_rejected(
        self, source_id: int, rej: ConversionRejection
    ) -> None:
        """変換棄却を業務 tx とは別 session で best-effort 監査する。

        業務 session に書くと後続 entry の source 全体失敗 (commit 前 rollback)
        で監査も巻き戻るため、必ず別 tx で commit する。DB 落ち / schema 不整合
        は log fallback (``failure_handling.py`` の 3 段防御 doctrine 再利用)。
        監査 repository には ``ConversionRejection`` ではなく原因例外のみ渡す。
        """
        try:
            async with self._session_factory() as audit_session:
                await SourceFetchAuditRepository(
                    audit_session
                ).append_conversion_rejected(
                    source_id=source_id,
                    exc=rej.error,
                    attempt=1,
                )
                await audit_session.commit()
        except Exception as audit_exc:
            logger.exception(
                "fetched_article_conversion_audit_dropped",
                source_id=source_id,
                business_error_class=(
                    f"{type(rej.error).__module__}.{type(rej.error).__qualname__}"
                ),
                business_error_message=redact_secrets(str(rej.error))[:500],
                audit_error_class=(
                    f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
                ),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
