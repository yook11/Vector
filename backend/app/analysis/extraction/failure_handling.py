"""Stage 3 の error handling policy を実行する application service。

Layer 1 marker (``NonRetryableDropArticle`` / ``NonRetryableKeepArticle`` /
``RetryableError`` / catch-all) を audit / DELETE / inline retry decision に
対応づける**唯一の場所**。Task 層は taskiq retry のために reraise decision
(``bool``) だけを解釈する。

Stage 3 固有要件 (失敗時に記事削除する Drop 経路) を持つため、Stage 4 / Stage 5
とは Handler を共有しない。Stage 4/5 の同型 Handler を導入する場合は別 PR。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.audit_repository import ExtractionAuditRepository
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.observability.categories import (
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)
from app.observability.redact import redact_secrets
from app.repositories.articles import ArticleRepository

logger = structlog.get_logger(__name__)

_DROP_FALLBACK_CODE = "ai_error_unknown_drop"


class ExtractionFailureHandler:
    """Stage 3 の失敗分類に応じた後処理を実行する application service。

    Drop 経路は audit + article DELETE の 1 tx を、それ以外は best-effort
    failure audit (DB 落ち時は log fallback) を実行する。INLINE_RETRY 判定は
    本 class が握り、結果を ``bool`` (taskiq に raise すべきかどうか) で返す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        ready: ReadyForExtraction,
        exc: BaseException,
        extractor: BaseExtractor,
        attempt: int,
        last_attempt: bool,
    ) -> bool:
        """marker dispatch を実行する。

        Returns:
            taskiq に raise すべきなら ``True``、return すべきなら ``False``。
        """
        match exc:
            case NonRetryableDropArticle():
                await self._drop_article(ready, exc, extractor)
                return False
            case NonRetryableKeepArticle():
                await self._audit_failure(ready, exc, extractor, attempt)
                return False
            case RetryableError():
                await self._audit_failure(ready, exc, extractor, attempt)
                return type(exc).INLINE_RETRY and not last_attempt
            case _:
                await self._audit_failure(ready, exc, extractor, attempt)
                return False

    async def _drop_article(
        self,
        ready: ReadyForExtraction,
        exc: BaseException,
        extractor: BaseExtractor,
    ) -> None:
        """内容起因 Permanent failure を 1 tx で焼付け + 記事 DELETE する。

        順序は **audit INSERT 先、DELETE 後** — ``source_id`` の自動逆引きが
        Article 存在中にしか動かないため。FK は ``ondelete=SET NULL`` 済で
        DELETE 後も audit 行は残る。
        """
        code = getattr(type(exc), "CODE", _DROP_FALLBACK_CODE)
        async with self._session_factory() as session:
            await ExtractionAuditRepository(session).append_drop_article(
                article_id=ready.article_id,
                original_content=ready.original_content,
                code=code,
                exc=exc,
                extractor=extractor,
            )
            deleted = await ArticleRepository(session).delete_by_id(ready.article_id)
            await session.commit()

        logger.warning(
            "extraction_article_unprocessable",
            article_id=ready.article_id,
            code=code,
            deleted_rows=deleted,
            error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )

    async def _audit_failure(
        self,
        ready: ReadyForExtraction,
        exc: BaseException,
        extractor: BaseExtractor,
        attempt: int,
    ) -> None:
        """best-effort failure audit (DB 落ち / schema 不整合は log fallback)。

        SDK exception message に key prefix / Authorization header が混入し
        うるため、log 経路にも ``redact_secrets`` を通す (red-team chain γ-2)。
        """
        try:
            async with self._session_factory() as session:
                await ExtractionAuditRepository(session).append_failure(
                    ready=ready,
                    exc=exc,
                    attempt=attempt,
                    extractor=extractor,
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "extraction_failure_audit_dropped",
                article_id=ready.article_id,
                attempt=attempt,
                business_error_class=(
                    f"{type(exc).__module__}.{type(exc).__qualname__}"
                ),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(
                    f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
                ),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
