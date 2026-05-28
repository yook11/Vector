"""Stage 3 の error handling policy を実行する application service。

Stage 3 Layer 1 marker (``CurationTerminalDropError`` /
``CurationTerminalKeepError`` / ``CurationRecoverableError`` / catch-all)
を audit / DELETE / taskiq retry decision に対応づける**唯一の場所**。Task 層
は taskiq retry / stage hold の decision だけを解釈する。

Stage 3 固有要件 (失敗時に記事削除する Drop 経路) を持つため、Stage 4 / Stage 5
とは Handler を共有しない。Stage 4/5 の同型 Handler を導入する場合は別 PR。
"""

from __future__ import annotations

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import AIProviderUsageLimitExhaustedError
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import (
    CurationError,
    CurationRecoverableError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
)
from app.analysis.failure_handling import FailureHandlingDecision
from app.audit.stages.curation import CurationAuditRepository
from app.repositories.articles import ArticleRepository
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)

_DROP_FALLBACK_CODE = "ai_error_unknown_drop"


class CurationFailureHandler:
    """Stage 3 の失敗分類に応じた後処理を実行する application service。

    Drop 経路は audit + article DELETE の 1 tx を、それ以外は best-effort
    failure audit (DB 落ち時は log fallback) を実行する。recoverable failure は
    taskiq retry に乗せる (``max_retries`` 上限後は cron 救済)、それ以外は即
    return する。結果を taskiq retry / stage hold の decision で返す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
        last_attempt: bool,
    ) -> FailureHandlingDecision:
        """marker dispatch を実行する。

        Returns:
            taskiq retry と stage hold の decision。
        """
        match exc:
            case CurationTerminalDropError():
                await self._drop_article(ready, exc, curator)
                return FailureHandlingDecision(reraise=False)
            case CurationTerminalKeepError():
                await self._audit_failure(ready, exc, curator)
                return FailureHandlingDecision(
                    reraise=False,
                    stage_hold_reason=getattr(exc, "code", "unknown"),
                )
            case CurationRecoverableError():
                recoverable = exc
                await self._audit_failure(ready, recoverable, curator)
                hold_reason = None
                if last_attempt and isinstance(
                    recoverable.provider_error,
                    AIProviderUsageLimitExhaustedError,
                ):
                    hold_reason = recoverable.code
                return FailureHandlingDecision(
                    reraise=not last_attempt,
                    stage_hold_reason=hold_reason,
                )
            case SQLAlchemyError():
                await self._audit_failure(ready, exc, curator)
                return FailureHandlingDecision(reraise=False)
            case _:
                await self._audit_unexpected_failure(ready, exc, curator)
                return FailureHandlingDecision(reraise=False)

    async def _drop_article(
        self,
        ready: ReadyForCuration,
        exc: CurationTerminalDropError,
        curator: BaseCurator,
    ) -> None:
        """内容起因 Permanent failure を 1 tx で焼付け + 記事 DELETE する。

        順序は **audit INSERT 先、DELETE 後** — ``source_id`` の自動逆引きが
        Article 存在中にしか動かないため。FK は ``ondelete=SET NULL`` 済で
        DELETE 後も audit 行は残る。
        """
        code = getattr(exc, "code", None) or _DROP_FALLBACK_CODE
        # audit INSERT → DELETE → commit を同一 tx で実行する。audit に失敗したら
        # DELETE も進まない構造を維持し、「削除だけ起きて audit が残らない」を避ける。
        async with self._session_factory() as session:
            await CurationAuditRepository(session).append_drop_article(
                ready=ready,
                code=code,
                exc=exc,
                curator=curator,
            )
            deleted = await ArticleRepository(session).delete_by_id(ready.article_id)
            await session.commit()

        logger.warning(
            "curation_article_unprocessable",
            article_id=ready.article_id,
            code=code,
            deleted_rows=deleted,
            error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )

    async def _audit_failure(
        self,
        ready: ReadyForCuration,
        exc: CurationError | SQLAlchemyError,
        curator: BaseCurator,
    ) -> None:
        """best-effort failure audit (DB 落ち / schema 不整合は log fallback)。

        SDK exception message に key prefix / Authorization header が混入し
        うるため、log 経路にも ``redact_secrets`` を通す (red-team chain γ-2)。
        """
        try:
            async with self._session_factory() as session:
                await CurationAuditRepository(session).append_failure(
                    ready=ready,
                    exc=exc,
                    curator=curator,
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "curation_failure_audit_dropped",
                article_id=ready.article_id,
                business_error_class=(
                    f"{type(exc).__module__}.{type(exc).__qualname__}"
                ),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(
                    f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
                ),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )

    async def _audit_unexpected_failure(
        self,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
    ) -> None:
        """想定外失敗の best-effort audit。"""
        try:
            async with self._session_factory() as session:
                await CurationAuditRepository(session).append_unexpected_failure(
                    ready=ready,
                    exc=exc,
                    curator=curator,
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "curation_failure_audit_dropped",
                article_id=ready.article_id,
                business_error_class=(
                    f"{type(exc).__module__}.{type(exc).__qualname__}"
                ),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(
                    f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
                ),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
