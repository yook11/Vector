"""Article Acquisition Service — 1 source 分のニュースから記事を獲得する。

収集 → 変換 → 永続化 の 3 工程を順に駆動する唯一のオーケストレータ:

1. **取得** ``fetch_articles(source, tools)`` engine が ``FetchedArticle`` を流す。
2. **変換** ``convert_fetched_article`` が「何ができたか」(Ready / Observed /
   棄却) に変換する。
3. **永続化** 変換結果を ``match`` で振り分ける:
   - ``AnalyzableArticle`` → 即時保存
   - ``ObservedArticle`` → ``incomplete_articles`` に投入 (後段補完)
   - ``ConversionRejection`` → 業務 tx とは別 session で棄却監査して継続

``commit`` までが責務。``NewsSource`` lookup は本 Service では行わない。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_acquisition.audit_repository import (
    SourceAcquisitionAuditRepository,
)
from app.collection.article_acquisition.errors import (
    SourceAcquisitionError,
    UnreadableResponseError,
)
from app.collection.article_acquisition.fetched_article_converter import (
    ConversionRejection,
    convert_fetched_article,
    unexpected_rejection,
)
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.persistence.article_store import ArticleStore
from app.collection.sources.article_source import ArticleSource
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)


class ArticleAcquisitionService:
    """1 source 分のニュースを取り込み、品質を担保した記事を獲得する。

    即時獲得は ``articles`` に保存、本文補完を要するものは
    ``incomplete_articles`` に保管 (後段 ``ArticleCompletionService``)。
    ソース全体の取得失敗は ``SourceAcquisitionError`` で Task に伝播する。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source: ArticleSource,
        tools_factory: Callable[[], ReaderTools] = ReaderTools,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._tools_factory = tools_factory

    async def execute(self, source_id: int) -> list[int]:
        async with self._session_factory() as session:
            article_store = ArticleStore(session)
            incomplete_repo = IncompleteArticleRepository(session)
            persisted_ids: list[int] = []
            tools = self._tools_factory()

            try:
                async for fetched in fetch_articles(self._source, tools):  # 1. 取得
                    try:
                        outcome = convert_fetched_article(  # 2. 変換 (= 何ができたか)
                            fetched, source=self._source, source_id=source_id
                        )
                    except Exception as exc:
                        # 想定外 bug のみ: convert 呼び出し 1 行だけを極小スコープで
                        # 値化する。collect 失敗 (外側 except) / persist 失敗 (try 外)
                        # と分離し、握りつぶし範囲を広げない。
                        outcome = unexpected_rejection(
                            fetched, source=self._source, cause=exc
                        )
                    match outcome:  # 3. 永続化 (DB error は try 外で素通り)
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
                            await incomplete_repo.save(
                                observed,
                                source_id=source_id,
                                ready_at=datetime.now(UTC),
                            )
                        case ConversionRejection() as rej:
                            # 変換不能 entry。業務 session には書かず別 tx で
                            # 監査して次 entry へ。
                            await self._audit_conversion_rejected(source_id, rej)
            except (ExternalFetchError, UnreadableResponseError) as exc:
                # read 失敗 (接続/読取) を CODE 付きで載せ替え伝播
                # (CODE は監査解像度用)。
                raise SourceAcquisitionError(str(exc), code=exc.CODE) from exc

            await session.commit()

        logger.info(
            "acquire_source_completed",
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
                await SourceAcquisitionAuditRepository(
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
