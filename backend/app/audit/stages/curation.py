"""Stage 3 curation の監査イベントを組み立てる。"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import TYPE_CHECKING, TypedDict

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.curation.ai.gemini_prompt import GeminiCurationPrompt
from app.analysis.prompt_safety import screen_untrusted_text
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import CurationPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import error_message_of, exception_fqn
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.injection_signal import record_injection_boundary_detected
from app.audit.ready_build import project_ready_build_failure
from app.audit.repository import PipelineEventRepository

if TYPE_CHECKING:
    from app.analysis.curation.ai.base import BaseCurator
    from app.analysis.curation.ai.envelope import CurationCall
    from app.analysis.curation.domain import Noise, Signal
    from app.analysis.curation.domain.ready import (
        CurationReadyBuildBlockedError,
        ReadyForCuration,
    )
    from app.analysis.curation.errors import CurationError, CurationTerminalDropError

logger = structlog.get_logger(__name__)

_AI_RAW_RESPONSE_LIMIT = 2048
_INPUT_CONTENT_HEAD_LIMIT = 2048
_INPUT_CONTENT_HASH_PREFIX_LEN = 16


class CurationOutcomeCode(StrEnum):
    """Stage.CURATION の outcome code (stage ファイル内定義分のみ)。"""

    BACKFILL_CURATION_AGED_OUT = "backfill_curation_aged_out"


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
    ) -> None:
        """signal 成功を記録する。"""
        content = _input_content_fields(ready.original_content)
        payload = CurationPayload(
            **content,
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
        if content["injection_markers_present"]:
            self._record_injection_detected(article_id=ready.article_id)

    async def append_noise(
        self,
        *,
        ready: ReadyForCuration,
        envelope: CurationCall[Noise],
        code: str,
    ) -> None:
        """noise 成功を記録する。"""
        content = _input_content_fields(ready.original_content)
        payload = CurationPayload(
            **content,
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
        if content["injection_markers_present"]:
            self._record_injection_detected(article_id=ready.article_id)

    # --- DROP 経路 (article DELETE と同一 tx) -----------------------------

    async def append_drop_article(
        self,
        *,
        ready: ReadyForCuration,
        code: str,
        exc: CurationTerminalDropError,
        curator: BaseCurator,
    ) -> None:
        """article 削除を伴う curation 失敗を記録する。"""
        projection = self._projection_of(exc, fallback_code=code)
        content = _input_content_fields(ready.original_content)
        payload = CurationPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            failure_reason=projection.failure_reason,
            # DROP は記事 DELETE と同一 tx で焼かれ FK article_id が SET NULL に
            # 落ちるため、削除に耐える記事識別子を payload に控える。
            target_article_id=ready.article_id,
            **content,
            ai_model=curator.model_name,
            prompt_version=curator.prompt_version,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            article_id=ready.article_id,
            error_class=exception_fqn(exc),
            retryability=projection.retryability,
        )
        if content["injection_markers_present"]:
            self._record_injection_detected(article_id=ready.article_id)

    # --- 救済断念経路 (年齢削除と同一 tx) ---------------------------------

    async def append_backfill_curation_aged_out(self, *, article_id: int) -> None:
        """古い未処理記事を backfill が諦めた事実を記録する。

        記事 DELETE と同一 tx で焼かれ FK article_id が SET NULL に落ちるため、
        削除に耐える記事識別子を payload に控える (これが無いと「どの記事か」が
        削除後に失われる)。
        """
        await self._events.append(
            stage=Stage.BACKFILL_CURATE,
            event_type=EventType.REJECTED,
            outcome_code=CurationOutcomeCode.BACKFILL_CURATION_AGED_OUT.value,
            payload=CurationPayload(target_article_id=article_id),
            article_id=article_id,
        )

    # --- Ready 構築 blocked / failed ---------------------------------------

    async def append_ready_build_blocked(
        self, *, target_article_id: int, exc: CurationReadyBuildBlockedError
    ) -> None:
        """Ready 構築が domain precondition により進めなかった事実を記録する。

        Domain が reason code で説明できた停止なので rejected として焼く。
        """
        payload = CurationPayload(
            target_article_id=target_article_id,
            input_content_length=exc.content_length,
            max_content_length=exc.max_content_length,
        )
        await self._events.append(
            stage=Stage.CURATION,
            event_type=EventType.REJECTED,
            outcome_code=exc.code.value,
            payload=payload,
            # 記事が現存する blocked (ALREADY_* / CONTENT_TOO_LARGE) のみ
            # article_id を運び source_id を補填する。ARTICLE_MISSING は対象
            # 記事が無く FK 不能なため None (sought id は payload.target_article_id)。
            article_id=exc.article_id,
        )

    async def append_ready_build_failed(
        self, *, target_article_id: int, exc: Exception
    ) -> None:
        """Ready 構築中に blocked 以外の例外が出た事実を failed として記録する。"""
        projection = project_ready_build_failure(stage_prefix="curation", exc=exc)
        payload = CurationPayload(
            failure_kind=projection.failure_kind,
            target_article_id=target_article_id,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=projection.outcome_code,
            payload=payload,
            error_class=exception_fqn(exc),
            retryability=Retryability.UNKNOWN,
        )

    # --- 失敗経路 (Task 層 4 marker dispatch) -----------------------------

    async def append_failure(
        self,
        *,
        ready: ReadyForCuration,
        exc: CurationError | SQLAlchemyError,
        curator: BaseCurator,
    ) -> None:
        """article を削除しない curation 失敗を記録する。"""
        projection = self._projection_of(exc)
        await self._append_failed_event(
            ready=ready,
            exc=exc,
            curator=curator,
            projection=projection,
        )

    async def append_unexpected_failure(
        self,
        *,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
    ) -> None:
        """想定外の curation 失敗を unknown として記録する。"""
        await self._append_failed_event(
            ready=ready,
            exc=exc,
            curator=curator,
            projection=unknown_failure_projection(),
        )

    async def _append_failed_event(
        self,
        *,
        ready: ReadyForCuration,
        exc: BaseException,
        curator: BaseCurator,
        projection: FailureProjection,
    ) -> None:
        content = _input_content_fields(ready.original_content)
        payload = CurationPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            failure_reason=projection.failure_reason,
            **content,
            ai_model=curator.model_name,
            prompt_version=curator.prompt_version,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            article_id=ready.article_id,
            error_class=exception_fqn(exc),
            retryability=projection.retryability,
        )
        if content["injection_markers_present"]:
            self._record_injection_detected(article_id=ready.article_id)

    # --- internal helpers -------------------------------------------------

    def _record_injection_detected(self, *, article_id: int | None) -> None:
        """injection 検知信号 (metric + log) を emit する。

        監査行の永続化 (``_events.append``) 成功後に呼ぶ。payload 構築や append が
        倒れた場合に signal だけ残る「metric +1 だが pipeline_events 行なし」の乖離
        を防ぐ。検知自体は境界タグ限定の高信号、無害化 (sanitize) とは別軸。
        """
        record_injection_boundary_detected(stage="curation")
        logger.warning(
            "audit_injection_boundary_detected",
            stage="curation",
            article_id=article_id,
        )

    @staticmethod
    def _projection_of(
        exc: BaseException, *, fallback_code: str = "unexpected_error"
    ) -> FailureProjection:
        """Stage 3 失敗を class attr / adapter から projection する。"""
        return project_failure(exc, fallback_code=fallback_code)


class _InputContentFields(TypedDict):
    """curation audit payload の input content snapshot + injection 検知フラグ。"""

    input_content_length: int
    input_content_head: str
    input_content_hash: str
    injection_markers_present: bool | None


def _input_content_fields(original_content: str) -> _InputContentFields:
    """curation audit payload の input content field と injection 信号を計算する。

    検知・無害化はともに LLM プロンプトへ渡る窓 (= 保存する窓) と同じ ``truncated``
    スライス上で行う。窓を超えた位置の境界タグは truncate で LLM に届かず無害なので
    検知しない (裏取り不能な false positive と、2 万字超記事での full scan を同時に
    避ける)。検知は境界タグ限定の高信号、無害化は sanitize による別軸。
    """
    truncated = original_content[: GeminiCurationPrompt.CONTENT_MAX_LENGTH]
    screening = screen_untrusted_text(truncated)
    return {
        "input_content_length": len(original_content),
        "input_content_head": screening.sanitized[:_INPUT_CONTENT_HEAD_LIMIT],
        "input_content_hash": hashlib.sha256(
            screening.sanitized.encode("utf-8")
        ).hexdigest()[:_INPUT_CONTENT_HASH_PREFIX_LEN],
        "injection_markers_present": screening.injection_detected or None,
    }
