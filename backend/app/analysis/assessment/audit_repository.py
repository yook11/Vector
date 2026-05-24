"""Stage 4 (assessment) 専用の pipeline_events 監査リポジトリ。

監査 row の **shape SSoT**。Service / Task / application helper は本 class の
semantic method を呼ぶだけで、``AssessmentPayload`` の組み立て・
``PipelineEventRepository.append()`` の引数列・``error_chain`` の FQN 組み立て
を一切知らない。

tx 境界は呼出側が握る (本 class は ``await session.commit()`` を呼ばない)。

設計 (案 3 = 厚い Ready 適用後):

- ``append_in_scope`` / ``append_out_of_scope`` は成功 audit で、Service の
  業務 INSERT と同 tx に焼く。``outcome_code`` (``"assessed_in_scope"`` /
  ``"assessed_out_of_scope"``) と ``category_slug`` は本 Repository 内で導出
  (caller は AI 境界型 ``InScope`` だけ渡す、固定文字列を持たない)
- ``append_failure`` は Task 層 3 marker dispatch 経路で別 session 別 tx として
  焼く (``exc`` から ``category`` / ``code`` を内部導出する SSoT)
- ``article_id`` / ``source_name`` は ``ReadyForAssessment`` が運ぶため
  AuditRepository 内での DB 逆引きは不要 (案 3 で 2-hop 逆引きを撤去)
- 構造差分 (vs Stage 3): Drop method なし (Stage 4 は drop_article 経路を持たない)
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.audit.categories import Layer1Category
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import AssessmentPayload
from app.audit.error_chain import extract_error_chain
from app.audit.repository import PipelineEventRepository
from app.shared.security.redaction import redact_secrets

_INPUT_TEXT_LIMIT = 4096  # spec §AssessmentPayload: input full 4KB
_AI_RAW_RESPONSE_LIMIT = 2048  # spec: ai_raw_response 2KB (Extraction と同値)
_ERROR_MESSAGE_LIMIT = 2000  # foundation 共通 (Extraction と同値)

_IN_SCOPE_OUTCOME_CODE = "assessed_in_scope"
_OUT_OF_SCOPE_OUTCOME_CODE = "assessed_out_of_scope"


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
        call: AssessmentCall[InScope],
    ) -> None:
        """in-scope 成功 audit を 1 行記録する。

        Service が業務 INSERT と同 tx で呼ぶ。Stage 4 で起きた全事実を抱える
        ``call`` envelope と Ready 構築時に取得済の参照値 (``ready.article_id`` /
        ``ready.source_name``) を組み合わせる。Domain Entity / DB 逆引きを介さない
        (`feedback_bc_boundary_guarantees_downstream` + 案 3)。

        audit は witness — 事後に採番された ``assessment_id`` / FK 解決後の
        ``category_id`` は事実ではなく操作的副産物なので payload に持たない
        (`specs/backlog/audit-payload-fact-purification.md`)。``curation_id``
        (自然キー) と ``category_slug`` (検証後 slug) で 1-hop join 可能。

        Args:
            call: ``AssessmentCall[InScope]`` envelope。``call.result``
                (= ``InScope``) から ``category.value`` (catalog 確認後 slug) /
                ``investor_take`` を、``call.model_name`` / ``call.prompt_version``
                / ``call.raw_*`` を audit 用に直接読む。
                ``raw_category`` (envelope 由来、validation 前生値) と
                ``category_slug`` (``result.category.value`` 由来) は意味分離する。
        """
        in_scope = call.result
        payload = AssessmentPayload(
            source_name=ready.source_name,
            curation_id=ready.curation_id,
            ai_model=call.model_name,
            prompt_version=call.prompt_version,
            input_text=ready.summary[:_INPUT_TEXT_LIMIT] or None,
            input_text_length=len(ready.summary),
            ai_raw_response=_limited_str(call.raw_response, _AI_RAW_RESPONSE_LIMIT),
            raw_category=call.raw_category,
            category_slug=in_scope.category.value,
            investor_take=in_scope.investor_take,
        )
        await self._events.append(
            stage=Stage.ASSESSMENT,
            event_type=EventType.SUCCEEDED,
            outcome_code=_IN_SCOPE_OUTCOME_CODE,
            payload=payload,
            article_id=ready.article_id,
            category=Layer1Category.SUCCESS,
            code=_IN_SCOPE_OUTCOME_CODE,
        )

    async def append_out_of_scope(
        self,
        *,
        ready: ReadyForAssessment,
        call: AssessmentCall[OutOfScope],
    ) -> None:
        """out-of-scope 成功 audit を 1 行記録する。

        PR #447 対称化以後、``out_of_scope_assessments`` テーブルも
        ``translated_title`` / ``summary`` / ``investor_take`` を保持する。
        audit payload もそれに追従し ``investor_take`` を焼く (本体 DB と
        audit の情報量を一致させる)。``category_slug`` は in-scope 固有のため
        out-of-scope では None。

        audit は witness — 事後に採番された ``assessment_id`` は事実ではない
        (`specs/backlog/audit-payload-fact-purification.md`)。``curation_id``
        (自然キー) で 1-hop join 可能。

        Args:
            call: ``AssessmentCall[OutOfScope]`` envelope。``call.result``
                (= ``OutOfScope``) から ``investor_take`` を、``call.model_name``
                / ``call.prompt_version`` / ``call.raw_*`` を audit 用に直接読む。
        """
        out_of_scope = call.result
        payload = AssessmentPayload(
            source_name=ready.source_name,
            curation_id=ready.curation_id,
            ai_model=call.model_name,
            prompt_version=call.prompt_version,
            input_text=ready.summary[:_INPUT_TEXT_LIMIT] or None,
            input_text_length=len(ready.summary),
            ai_raw_response=_limited_str(call.raw_response, _AI_RAW_RESPONSE_LIMIT),
            raw_category=call.raw_category,
            investor_take=out_of_scope.investor_take,
            # category_slug は in-scope 固有のため None
        )
        await self._events.append(
            stage=Stage.ASSESSMENT,
            event_type=EventType.SUCCEEDED,
            outcome_code=_OUT_OF_SCOPE_OUTCOME_CODE,
            payload=payload,
            article_id=ready.article_id,
            category=Layer1Category.SUCCESS,
            code=_OUT_OF_SCOPE_OUTCOME_CODE,
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
        Task 層 3 marker dispatch 経路から **別 session 別 tx** として呼ばれる
        (caller は ``tasks.py`` の task 関数末尾で別 session を開閉 + commit;
        PR4 で helper 廃止、task 末尾に inline)。
        commit は caller 側で行う (本 method は単一行 append のみ)。

        ``error_chain`` は ``error_chain.py::extract_error_chain`` を再利用して
        ``__cause__`` / ``__context__`` を辿る。
        ``raise map_provider_to_assessment(exc) from exc`` する想定のため、
        wrapper marker (``AssessmentRecoverableError``) と元 ``AIProviderError``
        の両方を payload に残す必要がある (Stage 3 は wrapper を挟まないため
        単発 ``[_fqn(exc)]`` で足りるが、Stage 4 では chain walking 必須)。
        """
        payload = AssessmentPayload(
            source_name=ready.source_name,
            curation_id=ready.curation_id,
            # red-team chain γ-2: SDK exception message に key prefix /
            # Authorization header が混入する経路を redact してから永続化
            # (Stage 3 と同 pattern)。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            # `raise X from exc` 連鎖を辿って FQN 列を payload に残す。
            error_chain=extract_error_chain(exc),
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
            article_id=ready.article_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    # --- internal helpers -------------------------------------------------

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

        provider 由来は ACL mapper (``errors.py`` Layer 2-A section) が
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
