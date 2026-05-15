"""Stage 1 (source_fetch) の error handling policy を実行する application service。

Layer 1 marker (``SourceFetchError``) と catch-all を audit / reraise decision に
対応づける唯一の場所。Task 層は taskiq の挙動 (raise すべきか) を ``bool`` で
受け取るだけで、marker の意味を知らずに済む。

Stage 1 は救済戦略が cron 一本化 (次 tick 再 dispatch、``max_retries=0``、
taskiq inline retry なし) のため ``last_attempt`` 概念を持たない。dispatch は
2 case:

- ``SourceFetchError`` (ソース全体の取得失敗) → audit を焼いて return (False)。
  30 分後の cron tick で再 dispatch される。
- catch-all (想定外 ``Exception``) → audit + ``logger.exception`` で worker log に
  可視化し reraise (True)。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.source_fetch.audit_repository import SourceFetchAuditRepository
from app.collection.source_fetch.errors import SourceFetchError
from app.observability.redact import redact_secrets

logger = structlog.get_logger(__name__)


class SourceFetchFailureHandler:
    """Stage 1 の失敗分類に応じた後処理を実行する application service。

    全 marker で best-effort failure audit (DB 落ち時は log fallback) を実行し、
    taskiq に raise すべきかどうかを ``bool`` で返す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        exc: BaseException,
        attempt: int,
    ) -> bool:
        """marker dispatch を実行する。

        Returns:
            taskiq に raise すべきなら ``True``、return すべきなら ``False``。
        """
        match exc:
            case SourceFetchError():
                await self._audit_failure(source_id, source_name, exc, attempt)
                return False
            case _:
                await self._audit_failure(source_id, source_name, exc, attempt)
                logger.exception(
                    "ingest_source_unexpected_error",
                    source_id=source_id,
                )
                return True

    async def _audit_failure(
        self,
        source_id: int | None,
        source_name: str | None,
        exc: BaseException,
        attempt: int,
    ) -> None:
        """best-effort failure audit (DB 落ち / schema 不整合は log fallback)。

        exception message に key prefix / Authorization header が混入しうるため、
        log 経路にも ``redact_secrets`` を通す (red-team chain γ-2、他 Stage と
        同 pattern)。
        """
        try:
            async with self._session_factory() as session:
                await SourceFetchAuditRepository(session).append_failure(
                    source_id=source_id,
                    source_name=source_name,
                    exc=exc,
                    attempt=attempt,
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "source_fetch_failure_audit_dropped",
                source_id=source_id,
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
