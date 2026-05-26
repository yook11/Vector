"""Briefing stage 専用の pipeline_events 監査リポジトリ。

監査 row の **shape SSoT**。Service / Task は本 class の semantic method を呼ぶだけ
で、``BriefingPayload`` の組み立て・``PipelineEventRepository.append()`` の引数列・
``error_chain`` の FQN 組み立てを一切知らない。

tx 境界は呼出側が握る (本 class は ``await session.commit()`` を呼ばない)。

設計:
- ``append_completed`` — subtask の成功 audit (Service が write tx 内で briefing
  UPSERT と同 tx に焼く、atomic)
- ``append_input_empty`` — subtask の入力ゼロ REJECTED (Service が read tx 直後の
  別 tx で焼く)
- ``append_failure`` — subtask の失敗 audit (Task 層 try/except から別 session
  別 tx で焼く、taskiq の retry / failure tracking を維持)
- ``append_dispatched`` — dispatcher の週次成功 anchor (全 subtask kiq 後)
- ``append_dispatcher_failure`` — dispatcher 自体が落ちたときの anchor
  (``broker_briefing`` は ``max_retries=0`` で初回即 give-up = 常に
  ``retry_exhausted=True``)

``category`` は exception class 由来の **intrinsic** な性質 (retry-friendly か否か)
を表し、retry 上限到達は payload 側の ``retry_exhausted: bool | None`` (extrinsic
な give-up timing) で別軸として持つ (``CompletionPayload`` precedent 同型)。

詳細: ``specs/pipeline-events-briefing-audit.md``
"""

from __future__ import annotations

from datetime import date

import openai
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.categories import Layer1Category
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BriefingPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    legacy_category_for_projection,
    project_db_failure,
    project_marker_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.models.category import Category
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000

# outcome_code 定数 (Service / Task / repo の wire 値 SSoT)。
OUTCOME_BRIEFING_COMPLETED = "briefing_completed"
OUTCOME_BRIEFING_INPUT_EMPTY = "briefing_input_empty"
OUTCOME_BRIEFING_DISPATCHED = "briefing_dispatched"


class BriefingAuditRepository:
    """Briefing stage 監査 row の semantic API。

    内部で ``PipelineEventRepository`` を compose し、generic な append SQL は
    そちらに委譲する。本 class の責務は **Briefing stage 固有の payload shape と
    Layer1Category / code の決定** に閉じる。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 (subtask) -----------------------------------------------

    async def append_completed(
        self,
        *,
        ready: ReadyForBriefing,
        article_count: int,
        ai_model: str,
    ) -> None:
        """subtask の成功 audit を 1 行記録する (caller は Service)。

        Service が write tx 内で briefing UPSERT 勝者に対してのみ呼ぶ (race 敗北は
        沈黙、勝者が焼く)。同 tx atomic で「briefing 行はあるが SUCCEEDED 無し」の
        偽ギャップを構造的に防ぐ。
        """
        category_slug = await self._resolve_category_slug(ready.category_id)
        payload = BriefingPayload(
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=category_slug,
            article_count=article_count,
            ai_model=ai_model,
        )
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.SUCCEEDED,
            outcome_code=OUTCOME_BRIEFING_COMPLETED,
            payload=payload,
            category=Layer1Category.SUCCESS,
            code=OUTCOME_BRIEFING_COMPLETED,
        )

    # --- REJECTED 経路 (subtask 入力ゼロ) -------------------------------

    async def append_input_empty(
        self,
        *,
        ready: ReadyForBriefing,
    ) -> None:
        """subtask の入力ゼロ REJECTED を 1 行記録する (steady-state 異常系)。

        記事ゼロは steady-state では起こり得ない異常系 (bootstrap 想定外)。
        ``event_type=REJECTED`` で完結し、``category`` は NULL (retry 概念外、
        failure path とは別軸)。

        Service が read tx 直後の別 tx で焼く (LLM 呼出も write tx も走らない)。
        """
        category_slug = await self._resolve_category_slug(ready.category_id)
        payload = BriefingPayload(
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=category_slug,
            article_count=0,
        )
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.REJECTED,
            outcome_code=OUTCOME_BRIEFING_INPUT_EMPTY,
            payload=payload,
            category=None,
            code=OUTCOME_BRIEFING_INPUT_EMPTY,
        )

    # --- 失敗経路 (subtask Task 層 try/except) ----------------------------

    async def append_failure(
        self,
        *,
        ready: ReadyForBriefing,
        exc: BaseException,
        attempt: int,
        retry_exhausted: bool | None,
        ai_model: str,
    ) -> None:
        """subtask の失敗 audit を 1 行記録する。

        ``category`` / ``code`` は ``exc`` から自動導出 (例外クラス由来の intrinsic
        な性質)。retry 上限到達は ``retry_exhausted`` (caller が
        ``is_last_attempt(ctx)`` 評価で渡す extrinsic な give-up timing) に格納。

        ``error_chain`` は ``extract_error_chain`` で ``__cause__`` を辿る。
        ``error_message`` は ``redact_secrets`` を通す (SDK exception の API key
        / Authorization header 混入経路を redact)。
        """
        category_slug = await self._resolve_category_slug(ready.category_id)
        payload = BriefingPayload(
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=category_slug,
            ai_model=ai_model,
            retry_exhausted=retry_exhausted,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        projection = self._projection_of(exc)
        category = legacy_category_for_projection(
            stage=Stage.BRIEFING, projection=projection
        )
        code = projection.code
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            attempt=attempt,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    # --- 成功経路 (dispatcher anchor) -------------------------------------

    async def append_dispatched(
        self,
        *,
        week_start: date,
        category_count: int,
    ) -> None:
        """dispatcher の週次成功 anchor を 1 行記録する。

        全 subtask kiq 後に dispatcher が焼く週 1 行のみのアンカー。dispatcher が
        落ちた週は subtask が一切 kiq されず痕跡ゼロになるため、「先週 briefing が
        動いたか」を SQL から確認する単一の証跡。per-category 軸 (category_id /
        category_slug / article_count) は埋めず、anchor 固有の ``category_count``
        のみ保持する。
        """
        payload = BriefingPayload(
            week_start=week_start.isoformat(),
            category_count=category_count,
        )
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.SUCCEEDED,
            outcome_code=OUTCOME_BRIEFING_DISPATCHED,
            payload=payload,
            category=Layer1Category.SUCCESS,
            code=OUTCOME_BRIEFING_DISPATCHED,
        )

    # --- 失敗経路 (dispatcher 自体の障害) ---------------------------------

    async def append_dispatcher_failure(
        self,
        *,
        week_start: date | None,
        exc: BaseException,
    ) -> None:
        """dispatcher 自体が落ちたときの anchor を 1 行記録する。

        ``broker_briefing`` の dispatcher は ``max_retries=0`` で初回が即 give-up
        のため ``retry_exhausted=True`` を固定で焼く。``week_start`` が決定する前に
        例外が出る可能性は低いが、防御的に ``None`` 許容で受ける。
        """
        payload = BriefingPayload(
            week_start=week_start.isoformat() if week_start is not None else None,
            retry_exhausted=True,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        projection = self._projection_of(exc)
        category = legacy_category_for_projection(
            stage=Stage.BRIEFING, projection=projection
        )
        code = projection.code
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    # --- internal helpers -------------------------------------------------

    async def _resolve_category_slug(self, category_id: int) -> str | None:
        """``category_id`` から ``categories.slug`` を引く (FK 切断耐性のため
        payload にも保存する)。``str`` 化して返す (Category.slug は VO のため)。
        """
        slug = await self._session.scalar(
            select(Category.slug).where(Category.id == category_id)
        )
        return str(slug) if slug is not None else None

    @staticmethod
    def _projection_of(exc: BaseException) -> FailureProjection:
        """Briefing 失敗を class attr / stage-local adapter から projection する。"""
        marker = project_marker_failure(exc)
        if marker is not None:
            return marker
        if isinstance(exc, ValidationError):
            return FailureProjection(
                failure_kind="response_invalid",
                retryability=Retryability.NON_RETRYABLE,
                failure_action=None,
                code="briefing_response_invalid",
            )
        db = project_db_failure(exc)
        if db is not None:
            return db
        if isinstance(exc, openai.APIError):
            return FailureProjection(
                failure_kind="llm_error",
                retryability=Retryability.RETRYABLE,
                failure_action=None,
                code="briefing_llm_error",
            )
        return unknown_failure_projection()

    @staticmethod
    def _category_of(exc: BaseException) -> Layer1Category:
        """互換用: projection から legacy ``category`` を導出する。"""
        return legacy_category_for_projection(
            stage=Stage.BRIEFING,
            projection=BriefingAuditRepository._projection_of(exc),
        )

    @staticmethod
    def _code_of(exc: BaseException) -> str:
        """互換用: projection から ``code`` を導出する。"""
        return BriefingAuditRepository._projection_of(exc).code


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
