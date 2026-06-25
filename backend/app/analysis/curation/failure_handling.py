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

from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import (
    CurationError,
    CurationRecoverableError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
)
from app.analysis.curation.metrics import record_curation_processing_outcome
from app.analysis.failure_handling import FailureHandlingDecision
from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.curation import CurationAuditRepository
from app.collection.persistence.analyzable_article_repository import (
    AnalyzableArticleRepository,
)
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)

_DROP_FALLBACK_CODE = "ai_error_unknown_drop"


def _hold_reason(
    exc: CurationRecoverableError | CurationTerminalKeepError,
) -> str | None:
    """provider error の回復クラスから stage hold reason を導出する。

    どの回復クラスが hold を要するかは ``AIProviderFailureMode.is_stage_hold_mode``
    が SSoT (marker 型には背負わせない)。hold reason には provider CODE
    (= ``exc.code``) を使い過去 hold metric との連続性を保つ。provider 由来でない
    失敗 (parse の ResponseInvalid 等) は hold しない。
    """
    provider_error = exc.provider_error
    if not isinstance(provider_error, AIProviderStateError | AIProviderContentError):
        return None
    return exc.code if provider_error.FAILURE_MODE.is_stage_hold_mode else None


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

        失敗分類を確定する境界として ``processing_outcome`` も emit する。
        SQLAlchemyError は infra_error (成功率の分母外)、それ以外は failed。
        recoverable は retry 有無に依らず試行単位で failed を数える。

        Returns:
            taskiq retry と stage hold の decision。
        """
        # 分類は match 時点で確定する。audit / drop の DB 失敗で metric を取りこぼさない
        # よう、副作用 (audit INSERT / 記事 DELETE) より先に emit する。
        match exc:
            case CurationTerminalDropError():
                record_curation_processing_outcome("failed")
                await self._drop_article(ready, exc, curator)
                return FailureHandlingDecision(reraise=False)
            case CurationTerminalKeepError():
                record_curation_processing_outcome("failed")
                await self._audit_failure(ready, exc, curator)
                return FailureHandlingDecision(
                    reraise=False,
                    stage_hold_reason=_hold_reason(exc),
                )
            case CurationRecoverableError():
                recoverable = exc
                record_curation_processing_outcome("failed")
                await self._audit_failure(ready, recoverable, curator)
                hold_reason = _hold_reason(recoverable) if last_attempt else None
                return FailureHandlingDecision(
                    reraise=not last_attempt,
                    stage_hold_reason=hold_reason,
                )
            case SQLAlchemyError():
                record_curation_processing_outcome("infra_error")
                await self._audit_failure(ready, exc, curator)
                return FailureHandlingDecision(reraise=False)
            case _:
                record_curation_processing_outcome("failed")
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
            deleted = await AnalyzableArticleRepository(session).delete_by_id(
                ready.analyzable_article_id
            )
            await session.commit()

        logger.warning(
            "curation_article_unprocessable",
            analyzable_article_id=ready.analyzable_article_id,
            code=code,
            deleted_rows=deleted,
            error_class=exception_fqn(exc),
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
                analyzable_article_id=ready.analyzable_article_id,
                business_error_class=(exception_fqn(exc)),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(exception_fqn(audit_exc)),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
            record_audit_dropped(Stage.CURATION)

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
                analyzable_article_id=ready.analyzable_article_id,
                business_error_class=(exception_fqn(exc)),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(exception_fqn(audit_exc)),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
            record_audit_dropped(Stage.CURATION)
