"""Stage 1 の失敗・棄却後処理を実行する service。"""

from __future__ import annotations

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.error_fields import exception_fqn
from app.audit.stages.acquisition import SourceAcquisitionAuditRepository
from app.collection.article_acquisition.errors import AcquisitionError
from app.collection.article_acquisition.fetched_article_converter import (
    AcquisitionConversionRejection,
)
from app.collection.article_acquisition.metrics import (
    AcquisitionEntryOutcome,
    record_acquisition_outcome,
)
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)


class ArticleAcquisitionFailureHandler:
    """Stage 1 の source-level failure と entry-level rejection を処理する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle_source_failure(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        exc: BaseException,
    ) -> bool:
        """taskiq に raise すべきなら ``True``、return すべきなら ``False``。"""
        match exc:
            case AcquisitionError():
                await self._audit_failure(source_id, source_name, exc)
                return False
            case SQLAlchemyError():
                await self._audit_failure(source_id, source_name, exc)
                return True
            case _:
                await self._audit_unexpected_failure(source_id, source_name, exc)
                logger.exception(
                    "acquire_source_unexpected_error",
                    source_id=source_id,
                )
                return True

    async def handle_conversion_rejected(
        self,
        source_id: int,
        rej: AcquisitionConversionRejection,
    ) -> None:
        """entry 単位の変換棄却を別 session で best-effort 監査し metric も出す。

        rejected の永続化境界はこの別 tx commit なので、監査行・redacted log・
        ``vector.acquisition.outcome{rejected}`` の 3 面をここで完結させる。metric は
        commit 成功時にだけ +1 し、監査行と件数を構造的に揃える (監査を best-effort で
        握りつぶした場合は計上せず redacted log に退避する)。
        """
        try:
            async with self._session_factory() as audit_session:
                await SourceAcquisitionAuditRepository(
                    audit_session
                ).append_conversion_rejected(
                    source_id=source_id,
                    rejection=rej,
                )
                await audit_session.commit()
        except Exception as audit_exc:
            logger.exception(
                "fetched_article_conversion_audit_dropped",
                source_id=source_id,
                business_outcome_code=rej.outcome_code,
                business_error_class=(
                    exception_fqn(rej.cause) if rej.cause is not None else None
                ),
                audit_error_class=(exception_fqn(audit_exc)),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
            return
        # metric add を try の外に置き、counter 失敗を監査 drop と混同しない。
        record_acquisition_outcome(AcquisitionEntryOutcome.REJECTED, count=1)

    async def _audit_failure(
        self,
        source_id: int | None,
        source_name: str | None,
        exc: AcquisitionError | SQLAlchemyError,
    ) -> None:
        """best-effort failure audit。失敗時は redacted log に退避する。"""
        try:
            async with self._session_factory() as session:
                await SourceAcquisitionAuditRepository(session).append_failure(
                    source_id=source_id,
                    source_name=source_name,
                    exc=exc,
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "source_acquisition_failure_audit_dropped",
                source_id=source_id,
                business_error_class=(exception_fqn(exc)),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(exception_fqn(audit_exc)),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )

    async def _audit_unexpected_failure(
        self,
        source_id: int | None,
        source_name: str | None,
        exc: BaseException,
    ) -> None:
        """想定外失敗の best-effort audit。"""
        try:
            async with self._session_factory() as session:
                await SourceAcquisitionAuditRepository(
                    session
                ).append_unexpected_failure(
                    source_id=source_id,
                    source_name=source_name,
                    exc=exc,
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "source_acquisition_failure_audit_dropped",
                source_id=source_id,
                business_error_class=(exception_fqn(exc)),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(exception_fqn(audit_exc)),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
