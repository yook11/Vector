"""Article Acquisition Service — 1 source 分のニュースから記事を獲得する。

Fetcher の stream を ``match`` で 3 型に振り分ける:

- ``AnalyzableArticle`` → 即時保存
- ``ObservedArticle`` → ``pending_html_articles`` に投入 (後段補完)
- ``ConversionRejection`` → 業務 tx とは別 session で棄却監査して継続

``commit`` までが責務。``NewsSource`` lookup は本 Service では行わない。
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

    即時獲得は ``articles`` に保存、本文補完を要するものは
    ``pending_html_articles`` に保管 (後段 ``ArticleCompletionService``)。
    ソース全体の取得失敗は ``SourceFetchError`` で Task に伝播する。
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
                            # 既知 URL の HTML fetch 反復を避けるコスト節約
                            # (UNIQUE は enqueue 側で担保)
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
                            # 変換不能 entry。業務 session には書かず別 tx で
                            # 監査して次 entry へ。
                            await self._audit_conversion_rejected(source_id, rej)
            except ExternalFetchError as exc:
                # tool 層で翻訳済の error を CODE 付きで載せ替え伝播
                # (CODE は監査解像度用)。
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

        業務 session に書くと後続の source 全体失敗で監査も巻き戻るため、
        必ず別 tx で commit する。失敗時は log fallback。
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
