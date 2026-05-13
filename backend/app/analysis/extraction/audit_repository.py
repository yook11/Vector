"""Stage 3 (extraction) 専用の pipeline_events 監査リポジトリ。

監査 row の **shape SSoT**。Service / Task / application helper は本 class の
semantic method を呼ぶだけで、``ExtractionPayload`` の組み立て・
``PipelineEventRepository.append()`` の引数列・``error_chain`` の FQN 組み立て
を一切知らない。

tx 境界は呼出側が握る (本 class は ``await session.commit()`` を呼ばない)。

設計:
- ``append_extracted`` / ``append_noise`` は成功 audit (caller である Service が
  ``"extracted"`` / ``"extracted_as_noise"`` の outcome code 文字列を ``code`` で
  渡す)。``ai_model`` / ``prompt_version`` / ``raw_relevance`` は envelope
  (``call.model_name`` / ``call.prompt_version`` / ``call.raw_relevance``) から
  直接埋める (Stage 4 ``append_in_scope`` / ``append_out_of_scope`` と対称)
- ``append_drop_article`` は ``mark_article_unprocessable`` 内で article DELETE
  と同一 tx に焼く (caller が ``type(exc).CODE`` を ``code`` で渡す)
- ``append_failure`` は Task 層 4 marker dispatch 経路で別 session 別 tx として
  焼く (``exc`` から ``category`` / ``code`` を内部導出する SSoT)

失敗経路 (``append_drop_article`` / ``append_failure``) は envelope を持たない
ため、``extractor: BaseExtractor`` を引数で受け、``extractor.model_name`` /
``extractor.prompt_version`` (property 経由) から ``ai_model`` /
``prompt_version`` を埋める (PR4 で ClassVar 強制を property 契約に置換、
Gemini hardcode 依存は引き続き持たない)。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.audit import base_extraction_payload_fields
from app.analysis.extraction.domain import Noise, Signal
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.models.article import Article
from app.models.news_source import NewsSource
from app.observability.categories import (
    Layer1Category,
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import ExtractionPayload
from app.observability.redact import redact_secrets
from app.observability.repository import PipelineEventRepository

_AI_RAW_RESPONSE_LIMIT = 2048
_ERROR_MESSAGE_LIMIT = 2000


class ExtractionAuditRepository:
    """Stage 3 監査 row の semantic API。

    内部で ``PipelineEventRepository`` を compose し、generic な append SQL は
    そちらに委譲する。本 class の責務は **Stage 3 固有の payload shape と
    Layer1Category / code の決定** に閉じる。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 ---------------------------------------------------------

    async def append_extracted(
        self,
        *,
        ready: ReadyForExtraction,
        envelope: ExtractionCall[Signal],
        code: str,
    ) -> None:
        """signal 経路の成功 audit を 1 行記録する。

        ``code`` は caller である Service が outcome 種別から渡す
        (例: ``"extracted"``)。``envelope`` は ``ExtractionCall[Signal]`` に
        narrow され、Service が ``match`` で振り分けた後にのみ呼ばれる。
        """
        source_name = await self._resolve_source_name(ready.article_id)
        payload = self._success_payload(ready, envelope, source_name)
        await self._events.append(
            stage=Stage.EXTRACTION,
            event_type=EventType.SUCCEEDED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
            category=Layer1Category.SUCCESS,
            code=code,
        )

    async def append_noise(
        self,
        *,
        ready: ReadyForExtraction,
        envelope: ExtractionCall[Noise],
        code: str,
    ) -> None:
        """noise 経路の成功 audit を 1 行記録する (``code="extracted_as_noise"``)。

        ``envelope`` は ``ExtractionCall[Noise]`` に narrow され、Service が
        ``match`` で振り分けた後にのみ呼ばれる。
        """
        source_name = await self._resolve_source_name(ready.article_id)
        payload = self._success_payload(ready, envelope, source_name)
        await self._events.append(
            stage=Stage.EXTRACTION,
            event_type=EventType.SUCCEEDED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
            category=Layer1Category.SUCCESS,
            code=code,
        )

    # --- DROP 経路 (article DELETE と同一 tx) -----------------------------

    async def append_drop_article(
        self,
        *,
        article_id: int,
        original_content: str,
        code: str,
        exc: BaseException,
        extractor: BaseExtractor,
    ) -> None:
        """``mark_article_unprocessable`` 内で article DELETE 直前に焼く audit。

        Service が同一 tx で DELETE と組み合わせる (本 class は commit しない)。
        ``code`` は ``type(exc).CODE`` (Layer 2 SSoT)、``category`` は固定で
        ``NON_RETRYABLE_DROP_ARTICLE``。

        失敗経路は envelope を持たない (AI 呼び出し前 or 中の失敗) ため
        ``ai_model`` / ``prompt_version`` は ``extractor`` の property
        (``model_name`` / ``prompt_version``) から埋める。
        """
        source_name = await self._resolve_source_name(article_id)
        payload = ExtractionPayload(
            **base_extraction_payload_fields(
                original_content=original_content,
                source_name=source_name,
            ),
            ai_model=extractor.model_name,
            prompt_version=extractor.prompt_version,
            # red-team chain γ-2: SDK exception message に key prefix /
            # Authorization header が混入する経路を redact してから永続化する。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=[_fqn(exc)],
        )
        await self._events.append(
            stage=Stage.EXTRACTION,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            article_id=article_id,
            error_class=_fqn(exc),
            category=Layer1Category.NON_RETRYABLE_DROP_ARTICLE,
            code=code,
        )

    # --- 失敗経路 (Task 層 4 marker dispatch) -----------------------------

    async def append_failure(
        self,
        *,
        ready: ReadyForExtraction,
        exc: BaseException,
        attempt: int,
        extractor: BaseExtractor,
    ) -> None:
        """NonRetryableKeepArticle / RetryableError / catch-all 経路の failure
        audit を 1 行記録する。

        ``category`` / ``code`` は ``exc`` から自動導出 (Layer 1 marker
        isinstance 分岐 + ``type(exc).CODE`` ClassVar 抽出)。Service と独立に
        Task 層から呼ばれるため別 session (caller が ``tasks.py`` の task 関数
        末尾で開閉 + commit する; PR4 で helper 廃止、task 末尾に inline)。

        失敗経路は envelope を持たない (AI 呼び出し前 or 中の失敗) ため
        ``ai_model`` / ``prompt_version`` は ``extractor`` の property
        (``model_name`` / ``prompt_version``) から埋める。
        """
        source_name = await self._resolve_source_name(ready.article_id)
        payload = ExtractionPayload(
            **base_extraction_payload_fields(
                original_content=ready.original_content,
                source_name=source_name,
            ),
            ai_model=extractor.model_name,
            prompt_version=extractor.prompt_version,
            # red-team chain γ-2: SDK exception message に key prefix /
            # Authorization header が混入する経路を redact してから永続化する。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=[_fqn(exc)],
        )
        category = self._category_of(exc)
        code = self._code_of(exc)
        await self._events.append(
            stage=Stage.EXTRACTION,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    # --- internal helpers -------------------------------------------------

    def _success_payload(
        self,
        ready: ReadyForExtraction,
        envelope: ExtractionCall[Signal] | ExtractionCall[Noise],
        source_name: str | None,
    ) -> ExtractionPayload:
        """成功経路 audit payload を envelope 経由で組み立てる。

        Stage 4 ``append_in_scope`` / ``append_out_of_scope`` と対称: ``ai_model``
        / ``prompt_version`` / ``raw_relevance`` は envelope から直接読み、Gemini
        ClassVar への静的依存を持たない
        (``feedback_bc_boundary_guarantees_downstream``)。``ai_raw_response`` は
        ``raw_response[:LIMIT]`` で切り詰める。``entity_count`` は
        ``envelope.result.entities`` の長さ。
        """
        return ExtractionPayload(
            **base_extraction_payload_fields(
                original_content=ready.original_content,
                source_name=source_name,
            ),
            ai_model=envelope.model_name,
            prompt_version=envelope.prompt_version,
            ai_raw_response=envelope.raw_response[:_AI_RAW_RESPONSE_LIMIT] or None,
            raw_relevance=envelope.raw_relevance,
            entity_count=len(envelope.result.entities),
        )

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
    def _category_of(exc: BaseException) -> Layer1Category:
        """Layer 1 marker から DB ``category`` 値を導出する (spec §原則 3)。"""
        if isinstance(exc, NonRetryableDropArticle):
            return Layer1Category.NON_RETRYABLE_DROP_ARTICLE
        if isinstance(exc, NonRetryableKeepArticle):
            return Layer1Category.NON_RETRYABLE_KEEP_ARTICLE
        if isinstance(exc, RetryableError):
            return Layer1Category.RETRYABLE
        return Layer1Category.UNKNOWN

    @staticmethod
    def _code_of(exc: BaseException) -> str:
        """Layer 2 ``CODE`` ClassVar を抽出する。未定義なら catch-all label。"""
        code = getattr(type(exc), "CODE", None)
        return code if isinstance(code, str) and code else "unexpected_error"


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
