"""Stage 4 (assessment) 専用の pipeline_events 監査リポジトリ。

監査 row の **shape SSoT**。Service / Task / application helper は本 class の
semantic method を呼ぶだけで、``AssessmentPayload`` の組み立て・
``PipelineEventRepository.append()`` の引数列・``error_chain`` の FQN 組み立て
を一切知らない。

tx 境界は呼出側が握る (本 class は ``await session.commit()`` を呼ばない)。

設計 (spec ``specs/pipeline-events-stage4-assessment.md`` §AssessmentAuditRepository):

- ``append_in_scope`` / ``append_out_of_scope`` は成功 audit (caller が
  ``"assessed_in_scope"`` / ``"assessed_out_of_scope"`` を ``code`` で渡す)、
  Service の業務 INSERT と同 tx に焼く (PR6 で wire-in)
- ``append_failure`` は Task 層 3 marker dispatch 経路で別 session 別 tx として
  焼く (``exc`` から ``category`` / ``code`` を内部導出する SSoT、PR6 で wire-in)
- 構造差分 (vs Stage 3): Drop method なし (Stage 4 は drop_article 経路を持たない)

PR5: caller 不在の dead code として merge。PR6 で Service / Task から呼ばれる。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.out_of_scope import OutOfScopeAssessment
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.analysis.classifier.envelope import AssessmentCall
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.news_source import NewsSource
from app.observability.categories import Layer1Category
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import AssessmentPayload
from app.observability.recording import _extract_error_chain
from app.observability.redact import redact_secrets
from app.observability.repository import PipelineEventRepository

_INPUT_TEXT_LIMIT = 4096  # spec §AssessmentPayload: input full 4KB
_AI_RAW_RESPONSE_LIMIT = 2048  # spec: ai_raw_response 2KB (Extraction と同値)
_ERROR_MESSAGE_LIMIT = 2000  # foundation 共通 (Extraction と同値)


def _limited_str(value: object, limit: int) -> str | None:
    """``value`` が非空 str ならば ``[:limit]`` で切詰、それ以外は ``None``。

    Audit 永続化前の長さ制御を 1 箇所に集約 (成功経路 ``envelope.raw_response`` /
    失敗経路 ``getattr(exc, "raw_response", None)`` の両方で使う)。失敗経路で
    限界を忘れて ``getattr(...)`` を裸で渡すと、将来 Layer 2-B exception が
    大きな ``raw_response`` instance attr を持った時に payload が肥大化する
    ため、helper として独立させる。
    """
    if isinstance(value, str) and value:
        return value[:limit]
    return None


class AssessmentAuditRepository:
    """Stage 4 監査 row の semantic API。

    内部で ``PipelineEventRepository`` を compose し、generic な append SQL は
    そちらに委譲する。本 class の責務は **Stage 4 固有の payload shape と
    Layer1Category / code の決定** に閉じる。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 (in-scope / out-of-scope の業務 INSERT と同 tx) ----------

    async def append_in_scope(
        self,
        *,
        ready: ReadyForAssessment,
        envelope: AssessmentCall,
        assessment: InScopeAssessment,
        ai_model: str,
        category_slug: str,
        code: str,
    ) -> None:
        """in-scope 成功 audit を 1 行記録する。

        PR6 で Service ``_handle_in_scope`` が業務 INSERT と同 tx で呼ぶ。

        Args:
            ai_model: ``classifier.model_name`` (BaseClassifier の ClassVar
                ``MODEL`` accessor) を caller が渡す。``AssessmentCall``
                envelope には ``model_name`` field が無い設計のため。
            category_slug: ``in_scope.category.value`` (parse 後の slug) を
                caller が渡す。``raw_category`` (envelope 由来、validation 前
                生値) と意味分離 — ``category_slug`` は catalog 確認後の slug。
            code: outcome 種別 (例 ``"assessed_in_scope"``)。
        """
        article_id = await self._article_id_for(ready.extraction_id)
        source_name = await self._resolve_source_name(ready.extraction_id)
        payload = AssessmentPayload(
            source_name=source_name,
            extraction_id=ready.extraction_id,
            ai_model=ai_model,
            prompt_version=envelope.prompt_version,
            input_text=ready.summary[:_INPUT_TEXT_LIMIT] or None,
            input_text_length=len(ready.summary),
            ai_raw_response=_limited_str(envelope.raw_response, _AI_RAW_RESPONSE_LIMIT),
            raw_category=envelope.raw_category,
            raw_topic=envelope.raw_topic,
            assessment_id=assessment.id,
            category_id=assessment.category_id,
            category_slug=category_slug,
            topic=str(assessment.topic),
            investor_take=assessment.investor_take,
        )
        await self._events.append(
            stage=Stage.ASSESSMENT,
            event_type=EventType.SUCCEEDED,
            outcome_code=code,
            payload=payload,
            article_id=article_id,
            category=Layer1Category.SUCCESS,
            code=code,
        )

    async def append_out_of_scope(
        self,
        *,
        ready: ReadyForAssessment,
        envelope: AssessmentCall,
        assessment: OutOfScopeAssessment,
        ai_model: str,
        code: str,
    ) -> None:
        """out-of-scope 成功 audit を 1 行記録する。

        spec 状態識別表 (line 962): out-of-scope では ``assessment_id`` のみ
        非 None、in-scope 系 field (``category_id`` / ``category_slug`` /
        ``topic`` / ``investor_take``) は全て None。``category_slug`` 引数も
        out-of-scope では取らない (caller が渡しても意味が無い)。
        """
        article_id = await self._article_id_for(ready.extraction_id)
        source_name = await self._resolve_source_name(ready.extraction_id)
        payload = AssessmentPayload(
            source_name=source_name,
            extraction_id=ready.extraction_id,
            ai_model=ai_model,
            prompt_version=envelope.prompt_version,
            input_text=ready.summary[:_INPUT_TEXT_LIMIT] or None,
            input_text_length=len(ready.summary),
            ai_raw_response=_limited_str(envelope.raw_response, _AI_RAW_RESPONSE_LIMIT),
            raw_category=envelope.raw_category,
            raw_topic=envelope.raw_topic,
            assessment_id=assessment.id,
            # in-scope 系 field は全て None (spec 状態識別表 line 962)
        )
        await self._events.append(
            stage=Stage.ASSESSMENT,
            event_type=EventType.SUCCEEDED,
            outcome_code=code,
            payload=payload,
            article_id=article_id,
            category=Layer1Category.SUCCESS,
            code=code,
        )

    # --- 失敗経路 (Task 層 3 marker dispatch、別 session 別 tx) ----------

    async def append_failure(
        self,
        *,
        ready: ReadyForAssessment,
        exc: BaseException,
        attempt: int,
    ) -> None:
        """3 marker dispatch 経路の failure audit を 1 行記録する。

        ``category`` / ``code`` は ``exc`` から自動導出 (Layer 1 marker
        ``isinstance`` 分岐 + instance 属性 ``exc.code`` 抽出)。Service と独立に
        Task 層から呼ばれるため別 session (caller は PR6 で実装する
        ``record_assessment_failure`` helper)。

        ``error_chain`` は ``recording.py::_extract_error_chain`` を再利用して
        ``__cause__`` / ``__context__`` を辿る。PR6 で
        ``raise map_provider_to_assessment(exc) from exc`` する想定のため、
        wrapper marker (``AssessmentRecoverableError``) と元 ``AIProviderError``
        の両方を payload に残す必要がある (Stage 3 は wrapper を挟まないため
        単発 ``[_fqn(exc)]`` で足りるが、Stage 4 では chain walking 必須)。
        """
        article_id = await self._article_id_for(ready.extraction_id)
        source_name = await self._resolve_source_name(ready.extraction_id)
        payload = AssessmentPayload(
            source_name=source_name,
            extraction_id=ready.extraction_id,
            # red-team chain γ-2: SDK exception message に key prefix /
            # Authorization header が混入する経路を redact してから永続化
            # (Stage 3 と同 pattern)。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            # PR6 想定の `raise X from exc` 連鎖を辿って FQN 列を payload に残す。
            error_chain=_extract_error_chain(exc),
            # parse 失敗 forensics: ``AssessmentResponseInvalidError`` 等が
            # 将来 ``raw_response`` instance attr を持った場合の hook
            # (現状 errors.py の Layer 2-B 群は raw_response を持たない、
            # None になる)。失敗経路でも _AI_RAW_RESPONSE_LIMIT で必ず切詰める
            # (成功経路と対称、helper ``_limited_str`` で集約)。
            ai_raw_response=_limited_str(
                getattr(exc, "raw_response", None), _AI_RAW_RESPONSE_LIMIT
            ),
        )
        category = self._category_of(exc)
        code = self._code_of(exc)
        await self._events.append(
            stage=Stage.ASSESSMENT,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            article_id=article_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    # --- internal helpers -------------------------------------------------

    async def _article_id_for(self, extraction_id: int) -> int | None:
        """``extraction_id`` から ``articles.id`` を逆引き。

        ``ReadyForAssessment`` は ``extraction_id`` のみ持ち ``article_id`` を
        持たない (taskiq in-flight message 互換のため field 不変、``ready.py``
        line 15-16 の注記)。``pipeline_events.article_id`` 列に詰めるため
        extractions テーブル経由で逆引きする。

        race で extractions row が消えていれば ``None`` (top-level column が
        NULL になるだけで audit 自体は記録される)。
        """
        stmt = select(ArticleExtraction.article_id).where(
            ArticleExtraction.id == extraction_id
        )
        return await self._session.scalar(stmt)

    async def _resolve_source_name(self, extraction_id: int) -> str | None:
        """``extraction_id`` 経由で ``news_sources.name`` を引く (FK 切断耐性)。

        2-hop join: extractions → articles → news_sources。Stage 3 の
        ``ExtractionAuditRepository._resolve_source_name(article_id)`` は
        1-hop だが、Stage 4 は ``ReadyForAssessment.article_id`` が無いため
        2-hop で引く。
        """
        stmt = (
            select(NewsSource.name)
            .join(Article, Article.source_id == NewsSource.id)
            .join(ArticleExtraction, ArticleExtraction.article_id == Article.id)
            .where(ArticleExtraction.id == extraction_id)
        )
        name = await self._session.scalar(stmt)
        return str(name) if name is not None else None

    @staticmethod
    def _category_of(exc: BaseException) -> Layer1Category:
        """Layer 1 marker から DB ``category`` 値を導出する
        (spec §_category_of line 1081-1089)。

        Stage 4 の意図的命名差: ``AssessmentTerminalSkipError`` は
        ``Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION`` にマップする
        (extraction を捨てない、article 保持の最も保守的な意味)。

        dispatch 順は TerminalSkip → Recoverable → fallback。Layer 2-B は
        対応する Layer 1 marker (``Recoverable`` or ``TerminalSkip``) を継承
        するため、より固有な class を先に判定する必要は無いが、明示性のため
        Stage 3 と同じく specific-first で並べる。
        """
        if isinstance(exc, AssessmentTerminalSkipError):
            return Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION
        if isinstance(exc, AssessmentRecoverableError):
            return Layer1Category.RETRYABLE
        return Layer1Category.UNKNOWN

    @staticmethod
    def _code_of(exc: BaseException) -> str:
        """Stage 4 marker の **instance 属性** ``code`` を抽出する
        (spec §_code_of line 1091-1095)。

        Stage 3 (``ExtractionAuditRepository._code_of``) は ClassVar ``CODE``
        (``getattr(type(exc), "CODE", None)``) を見るが、Stage 4 marker は
        ctor で ``code: str`` を必須キーワードとして受け instance attr に
        保持する設計
        (PR1 で導入、``backend/app/analysis/assessment/errors.py``)。

        provider 由来は ACL mapper (``provider_mapping.py``) が
        ``AIProviderError.CODE`` を引き継いで instance attr に詰めるため、
        本 method は instance 経路のみで全パターン (Layer 2-A: provider mapped /
        Layer 2-B: ``AssessmentResponseInvalidError`` ctor 内 hardcode
        ``"assessment_response_invalid"`` / ``AssessmentCategoryMissingError``
        ctor 内 hardcode ``"assessment_category_missing"`` / catch-all:
        ``Exception``) をカバーできる。

        未定義 / 空文字 / 非 str は catch-all label に fallback。
        """
        code = getattr(exc, "code", None)
        return code if isinstance(code, str) and code else "unexpected_error"


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
