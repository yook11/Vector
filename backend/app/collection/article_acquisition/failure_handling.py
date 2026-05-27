"""Stage 1 の失敗・棄却後処理を実行する service。"""

from __future__ import annotations

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.acquisition import SourceAcquisitionAuditRepository
from app.collection.article_acquisition.errors import SourceAcquisitionError
from app.collection.article_acquisition.fetched_article_converter import (
    ConversionRejection,
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
            case SourceAcquisitionError():
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
        rej: ConversionRejection,
    ) -> None:
        """entry 単位の変換棄却を別 session で best-effort 監査する。"""
        try:
            async with self._session_factory() as audit_session:
                await SourceAcquisitionAuditRepository(
                    audit_session
                ).append_conversion_rejected(
                    source_id=source_id,
                    exc=rej.error,
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

    async def _audit_failure(
        self,
        source_id: int | None,
        source_name: str | None,
        exc: SourceAcquisitionError | SQLAlchemyError,
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
                business_error_class=(
                    f"{type(exc).__module__}.{type(exc).__qualname__}"
                ),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(
                    f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
                ),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
