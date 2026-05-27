"""Stage 5 の error handling policy を実行する application service。

Layer 1 marker (``EmbeddingTerminalError`` / ``EmbeddingRecoverableError`` /
catch-all) を audit / inline retry decision に対応づける唯一の場所。Task 層は
taskiq retry のために reraise decision (``bool``) だけを解釈する。

Stage 5 は内容起因 DELETE 経路を持たない (analysis を保持して embedding を
skip する設計) ため、Stage 4 と Handler は共有しない (Stage 4 PR #497 と
完全同形、ID 軸 / marker / event 名のみ差替)。
"""

from __future__ import annotations

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingRecoverableError,
    EmbeddingTerminalError,
    EmbeddingTerminalStageBlockedError,
)
from app.analysis.embedding.hold import set_embedding_hold
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.redis import get_redis
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)


class EmbeddingFailureHandler:
    """Stage 5 の失敗分類に応じた後処理を実行する application service。

    全 marker で best-effort failure audit (DB 落ち時は log fallback) を実行し、
    taskiq に raise すべきかどうかを ``bool`` で返す。branch ごとの分類ログも
    本 class 内で完結させ、task 層は marker の意味を知らずに済む。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        ready: ReadyForEmbedding,
        exc: BaseException,
        last_attempt: bool,
    ) -> bool:
        """marker dispatch を実行する。

        Returns:
            taskiq に raise すべきなら ``True``、return すべきなら ``False``。
        """
        match exc:
            case EmbeddingTerminalStageBlockedError():
                logger.warning(
                    "generate_embedding_terminal_stage_blocked",
                    analysis_id=ready.analysis_id,
                    code=getattr(exc, "code", None),
                )
                await self._audit_failure(ready, exc)
                await set_embedding_hold(
                    get_redis(), reason=getattr(exc, "code", "unknown")
                )
                return False
            case EmbeddingTerminalError():
                logger.warning(
                    "generate_embedding_terminal",
                    analysis_id=ready.analysis_id,
                    code=getattr(exc, "code", None),
                )
                await self._audit_failure(ready, exc)
                return False
            case EmbeddingRecoverableError():
                await self._audit_failure(ready, exc)
                if last_attempt:
                    logger.warning(
                        "generate_embedding_recoverable_exhausted",
                        analysis_id=ready.analysis_id,
                        code=getattr(exc, "code", None),
                    )
                    return False
                return True
            case SQLAlchemyError():
                await self._audit_failure(ready, exc)
                return not last_attempt
            case _:
                await self._audit_unexpected_failure(ready, exc)
                if last_attempt:
                    logger.exception(
                        "generate_embedding_unexpected_exhausted",
                        analysis_id=ready.analysis_id,
                    )
                    return False
                return True

    async def _audit_failure(
        self,
        ready: ReadyForEmbedding,
        exc: EmbeddingError | SQLAlchemyError,
    ) -> None:
        """best-effort failure audit (DB 落ち / schema 不整合は log fallback)。

        SDK exception message に key prefix / Authorization header が混入し
        うるため、log 経路にも ``redact_secrets`` を通す (red-team chain γ-2、
        Stage 3 / Stage 4 と同 pattern)。
        """
        try:
            async with self._session_factory() as session:
                await EmbeddingAuditRepository(session).append_failure(
                    ready=ready, exc=exc
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "embedding_failure_audit_dropped",
                analysis_id=ready.analysis_id,
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
        ready: ReadyForEmbedding,
        exc: BaseException,
    ) -> None:
        """想定外失敗の best-effort audit。"""
        try:
            async with self._session_factory() as session:
                await EmbeddingAuditRepository(session).append_unexpected_failure(
                    ready=ready, exc=exc
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "embedding_failure_audit_dropped",
                analysis_id=ready.analysis_id,
                business_error_class=(
                    f"{type(exc).__module__}.{type(exc).__qualname__}"
                ),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(
                    f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
                ),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
