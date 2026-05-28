"""Stage 3 curation の監査イベントを組み立てる。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.curation.domain.ready import CurationReadyBuildBlockedCode
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import CurationPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.ready_build import project_ready_build_failure
from app.audit.repository import PipelineEventRepository
from app.models.article import Article
from app.models.news_source import NewsSource
from app.shared.security.redaction import redact_secrets

if TYPE_CHECKING:
    from app.analysis.curation.ai.base import BaseCurator
    from app.analysis.curation.ai.envelope import CurationCall
    from app.analysis.curation.domain import Noise, Signal
    from app.analysis.curation.domain.ready import (
        CurationReadyBuildBlocked,
        ReadyForCuration,
    )
    from app.analysis.curation.errors import CurationError, CurationTerminalDropError

_AI_RAW_RESPONSE_LIMIT = 2048
_ERROR_MESSAGE_LIMIT = 2000

BACKFILL_CURATION_AGED_OUT_CODE = "backfill_curation_aged_out"

_READY_BUILD_BLOCKED_CODES = {
    CurationReadyBuildBlockedCode.ARTICLE_MISSING: (
        "curation_ready_build_blocked_article_missing"
    ),
    CurationReadyBuildBlockedCode.ALREADY_CURATED: (
        "curation_ready_build_blocked_already_curated"
    ),
    CurationReadyBuildBlockedCode.ALREADY_REJECTED_AS_NOISE: (
        "curation_ready_build_blocked_already_rejected_as_noise"
    ),
    CurationReadyBuildBlockedCode.CONTENT_TOO_LARGE: (
        "curation_ready_build_blocked_content_too_large"
    ),
}


class CurationAuditRepository:
    """Stage 3 専用の payload / outcome_code / failure projection を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 ---------------------------------------------------------

    async def append_signal(
        self,
        *,
        ready: ReadyForCuration,
        envelope: CurationCall[Signal],
        code: str,
        input_content_length: int,
        input_content_head: str,
        input_content_hash: str,
    ) -> None:
        """signal 成功を記録する。"""
        payload = CurationPayload(
            source_name=await self._resolve_source_name(ready.article_id),
            input_content_length=input_content_length,
            input_content_head=input_content_head,
            input_content_hash=input_content_hash,
            ai_model=envelope.model_name,
            prompt_version=envelope.prompt_version,
            ai_raw_response=envelope.raw_response[:_AI_RAW_RESPONSE_LIMIT] or None,
            raw_relevance=envelope.raw_relevance,
        )
        await self._events.append(
            stage=Stage.CURATION,
            event_type=EventType.SUCCEEDED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
        )

    async def append_noise(
        self,
        *,
        ready: ReadyForCuration,
        envelope: CurationCall[Noise],
        code: str,
        input_content_length: int,
        input_content_head: str,
        input_content_hash: str,
    ) -> None:
        """noise 成功を記録する。"""
        payload = CurationPayload(
            source_name=await self._resolve_source_name(ready.article_id),
            input_content_length=input_content_length,
            input_content_head=input_content_head,
            input_content_hash=input_content_hash,
            ai_model=envelope.model_name,
            prompt_version=envelope.prompt_version,
            ai_raw_response=envelope.raw_response[:_AI_RAW_RESPONSE_LIMIT] or None,
            raw_relevance=envelope.raw_relevance,
        )
        await self._events.append(
            stage=Stage.CURATION,
            event_type=EventType.SUCCEEDED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
        )

    # --- DROP 経路 (article DELETE と同一 tx) -----------------------------

    async def append_drop_article(
        self,
        *,
        article_id: int,
        code: str,
        exc: CurationTerminalDropError,
        curator: BaseCurator,
        input_content_length: int,
        input_content_head: str,
        input_content_hash: str,
    ) -> None:
        """article 削除を伴う curation 失敗を記録する。"""
        projection = self._projection_of(exc, fallback_code=code)
        payload = CurationPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            source_name=await self._resolve_source_name(article_id),
            input_content_length=input_content_length,
            input_content_head=input_content_head,
            input_content_hash=input_content_hash,
            ai_model=curator.model_name,
            prompt_version=curator.prompt_version,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            article_id=article_id,
            error_class=_fqn(exc),
            retryability=projection.retryability,
        )

    # --- 救済断念経路 (年齢削除と同一 tx) ---------------------------------

    async def append_backfill_curation_aged_out(self, *, article_id: int) -> None:
        """古い未処理記事を backfill が諦めた事実を記録する。"""
        source_name = await self._resolve_source_name(article_id)
        await self._events.append(
            stage=Stage.BACKFILL_CURATE,
            event_type=EventType.REJECTED,
            outcome_code=BACKFILL_CURATION_AGED_OUT_CODE,
            payload=CurationPayload(source_name=source_name),
            article_id=article_id,
        )

    # --- Ready 構築 blocked / failed ---------------------------------------

    async def append_ready_build_blocked(
        self, *, blocked: CurationReadyBuildBlocked
    ) -> None:
        """Ready 構築が業務状態により対象外だった事実を記録する。"""
        payload = CurationPayload(
            source_name=blocked.source_name,
            target_article_id=blocked.target_article_id,
            input_content_length=blocked.content_length,
            max_content_length=blocked.max_content_length,
        )
        article_id = (
            None
            if blocked.code is CurationReadyBuildBlockedCode.ARTICLE_MISSING
            else blocked.target_article_id
        )
        await self._events.append(
            stage=Stage.CURATION,
            event_type=EventType.REJECTED,
            outcome_code=_READY_BUILD_BLOCKED_CODES[blocked.code],
            payload=payload,
            article_id=article_id,
        )

    async def append_ready_build_failed(
        self, *, target_article_id: int, exc: Exception
    ) -> None:
        """Ready 構築フェーズの例外を failed として記録する。"""
        projection = project_ready_build_failure(stage_prefix="curation", exc=exc)
        payload = CurationPayload(
            failure_kind=projection.failure_kind,
            target_article_id=target_article_id,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=projection.outcome_code,
            payload=payload,
            error_class=_fqn(exc),
            retryability=Retryability.UNKNOWN,
        )

    # --- 失敗経路 (Task 層 4 marker dispatch) -----------------------------

    async def append_failure(
        self,
        *,
        ready: ReadyForCuration,
        exc: CurationError | SQLAlchemyError,
        curator: BaseCurator,
        input_content_length: int,
        input_content_head: str,
        input_content_hash: str,
    ) -> None:
        """article を削除しない curation 失敗を記録する。"""
        projection = self._projection_of(exc)
        await self._append_failed_event(
            ready=ready,
            exc=exc,
            curator=curator,
            input_content_length=input_content_length,
            input_content_head=input_content_head,
            input_content_hash=input_content_hash,
            projection=projection,
        )

    async def append_unexpected_failure(
        self,
        *,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
        input_content_length: int,
        input_content_head: str,
        input_content_hash: str,
    ) -> None:
        """想定外の curation 失敗を unknown として記録する。"""
        await self._append_failed_event(
            ready=ready,
            exc=exc,
            curator=curator,
            input_content_length=input_content_length,
            input_content_head=input_content_head,
            input_content_hash=input_content_hash,
            projection=unknown_failure_projection(),
        )

    async def _append_failed_event(
        self,
        *,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
        input_content_length: int,
        input_content_head: str,
        input_content_hash: str,
        projection: FailureProjection,
    ) -> None:
        payload = CurationPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            source_name=await self._resolve_source_name(ready.article_id),
            input_content_length=input_content_length,
            input_content_head=input_content_head,
            input_content_hash=input_content_hash,
            ai_model=curator.model_name,
            prompt_version=curator.prompt_version,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            article_id=ready.article_id,
            error_class=_fqn(exc),
            retryability=projection.retryability,
        )

    # --- internal helpers -------------------------------------------------

    async def _resolve_source_name(self, article_id: int) -> str | None:
        """``article_id`` から ``news_sources.name`` を引く (FK 切断耐性のため
        payload にも保存する)。``str`` 化して返す (NewsSource.name は VO のため)。
        """
        stmt = (
            select(NewsSource.name)
            .join(Article, Article.source_id == NewsSource.id)
            .where(Article.id == article_id)
        )
        name = await self._session.scalar(stmt)
        return str(name) if name is not None else None

    @staticmethod
    def _projection_of(
        exc: BaseException, *, fallback_code: str = "unexpected_error"
    ) -> FailureProjection:
        """Stage 3 失敗を class attr / adapter から projection する。"""
        return project_failure(exc, fallback_code=fallback_code)


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
