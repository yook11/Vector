"""Stage 3 の error handling policy を実行する application service。

Stage 3 Layer 1 marker (``CurationTerminalDropError`` /
``CurationTerminalKeepError`` / ``CurationRecoverableError`` / catch-all)
を audit / DELETE / taskiq retry decision に対応づける**唯一の場所**。Task 層
は taskiq retry のために reraise decision (``bool``) だけを解釈する。

Stage 3 固有要件 (失敗時に記事削除する Drop 経路) を持つため、Stage 4 / Stage 5
とは Handler を共有しない。Stage 4/5 の同型 Handler を導入する場合は別 PR。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.audit_repository import CurationAuditRepository
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import (
    CurationRecoverableError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
)
from app.analysis.curation.hold import set_curation_hold
from app.redis import get_redis
from app.repositories.articles import ArticleRepository
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)

_DROP_FALLBACK_CODE = "ai_error_unknown_drop"


class CurationFailureHandler:
    """Stage 3 の失敗分類に応じた後処理を実行する application service。

    Drop 経路は audit + article DELETE の 1 tx を、それ以外は best-effort
    failure audit (DB 落ち時は log fallback) を実行する。recoverable failure は
    taskiq retry に乗せる (``max_retries`` 上限後は cron 救済)、それ以外は即
    return する。結果を ``bool`` (taskiq に raise すべきかどうか) で返す。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
        attempt: int,
        last_attempt: bool,
    ) -> bool:
        """marker dispatch を実行する。

        Returns:
            taskiq に raise すべきなら ``True``、return すべきなら ``False``。
        """
        match exc:
            case CurationTerminalDropError():
                await self._drop_article(ready, exc, curator)
                return False
            case CurationTerminalKeepError():
                await self._audit_failure(ready, exc, curator, attempt)
                # provider/stage 全体の健全性問題。backfill 再投入を一時停止する
                # (hold は best-effort、set 失敗でも task は落とさない)。
                await set_curation_hold(
                    get_redis(), reason=getattr(exc, "code", "unknown")
                )
                return False
            case CurationRecoverableError():
                await self._audit_failure(ready, exc, curator, attempt)
                return not last_attempt
            case _:
                await self._audit_failure(ready, exc, curator, attempt)
                return False

    async def _drop_article(
        self,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
    ) -> None:
        """内容起因 Permanent failure を 1 tx で焼付け + 記事 DELETE する。

        順序は **audit INSERT 先、DELETE 後** — ``source_id`` の自動逆引きが
        Article 存在中にしか動かないため。FK は ``ondelete=SET NULL`` 済で
        DELETE 後も audit 行は残る。
        """
        code = getattr(exc, "code", None) or _DROP_FALLBACK_CODE
        async with self._session_factory() as session:
            await CurationAuditRepository(session).append_drop_article(
                article_id=ready.article_id,
                original_content=ready.original_content,
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
        exc: BaseException,
        curator: BaseCurator,
        attempt: int,
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
                    attempt=attempt,
                    curator=curator,
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "curation_failure_audit_dropped",
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
