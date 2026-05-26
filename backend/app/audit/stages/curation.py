"""Stage 3 (curation) 専用の pipeline_events 監査リポジトリ。

監査 row の **shape SSoT**。Service / Task / application helper は本 class の
semantic method を呼ぶだけで、``CurationPayload`` の組み立て・
``PipelineEventRepository.append()`` の引数列・``error_chain`` の FQN 組み立て
を一切知らない。

tx 境界は呼出側が握る (本 class は ``await session.commit()`` を呼ばない)。

設計:
- ``append_signal`` / ``append_noise`` は成功 audit (caller である Service が
  ``"curated_signal"`` / ``"curated_noise"`` の outcome code 文字列を ``code`` で
  渡す)。``ai_model`` / ``prompt_version`` / ``raw_relevance`` は envelope
  (``call.model_name`` / ``call.prompt_version`` / ``call.raw_relevance``) から
  直接埋める (Stage 4 ``append_in_scope`` / ``append_out_of_scope`` と対称)
- ``append_drop_article`` は ``mark_article_unprocessable`` 内で article DELETE
  と同一 tx に焼く (caller が ``type(exc).CODE`` を ``code`` で渡す)
- ``append_failure`` は Task 層 4 marker dispatch 経路で別 session 別 tx として
  焼く (``exc`` から ``category`` / ``code`` を内部導出する SSoT)

失敗経路 (``append_drop_article`` / ``append_failure``) は envelope を持たない
ため、``curator: BaseCurator`` を引数で受け、``curator.model_name`` /
``curator.prompt_version`` (property 経由) から ``ai_model`` /
``prompt_version`` を埋める (PR4 で ClassVar 強制を property 契約に置換、
Gemini hardcode 依存は引き続き持たない)。

PR-E.1.5 で ``Stage.CURATION`` / ``CurationPayload`` に rename 済み (旧
``extraction`` wire 値は migration z1_curation_completion_rename で移行)。

逆依存の暫定許容 (本 PR 一時的):
    本 module は curation context 内の helper
    ``app.analysis.curation.audit.base_curation_payload_fields`` に依存する
    (helper は ``GeminiCurationPrompt.CONTENT_MAX_LENGTH`` /
    ``sanitize_for_untrusted_block`` への参照を内包するため curation 側に残した方が
    cohesive)。これは ``app/audit/`` → ``app/analysis/curation/`` の逆方向 import を
    一時的に許容している (``app/audit/__init__.py`` の依存方向宣言と矛盾)。

    別 PR で「caller pre-compute (案 C)」に置換する宿題:
    curation Service / failure_handling / backfill task 側で
    ``base_curation_payload_fields(...)`` を事前算出し、
    ``CurationAuditRepository.append_*`` に kwargs で渡す。これにより audit context は
    curation domain を import しなくなり、依存方向が完全に片方向化する。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.audit import base_curation_payload_fields
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import (
    CurationRecoverableError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
)
from app.audit.categories import Layer1Category
from app.audit.db_errors import DbErrorCause, classify_db_error
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import CurationPayload
from app.audit.error_chain import extract_error_chain
from app.audit.repository import PipelineEventRepository
from app.models.article import Article
from app.models.news_source import NewsSource
from app.shared.security.redaction import redact_secrets

_AI_RAW_RESPONSE_LIMIT = 2048
_ERROR_MESSAGE_LIMIT = 2000

# 年齢起因の救済断念 (backfill が古い未処理記事を物理削除) の outcome code。
# 内容拒否の drop (NON_RETRYABLE_DROP_ARTICLE) とは性質が全く異なるため別 code。
BACKFILL_CURATION_AGED_OUT_CODE = "backfill_curation_aged_out"


class CurationAuditRepository:
    """Stage 3 監査 row の semantic API。

    内部で ``PipelineEventRepository`` を compose し、generic な append SQL は
    そちらに委譲する。本 class の責務は **Stage 3 固有の payload shape と
    Layer1Category / code の決定** に閉じる。
    """

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
        """signal 経路の成功 audit を 1 行記録する。

        ``code`` は caller である Service が outcome 種別から渡す
        (例: ``"curated_signal"``)。``envelope`` は ``CurationCall[Signal]`` に
        narrow され、Service が ``match`` で振り分けた後にのみ呼ばれる。
        """
        source_name = await self._resolve_source_name(ready.article_id)
        payload = self._success_payload(ready, envelope, source_name)
        await self._events.append(
            stage=Stage.CURATION,
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
        ready: ReadyForCuration,
        envelope: CurationCall[Noise],
        code: str,
    ) -> None:
        """noise 経路の成功 audit を 1 行記録する (``code="curated_noise"``)。

        ``envelope`` は ``CurationCall[Noise]`` に narrow され、Service が
        ``match`` で振り分けた後にのみ呼ばれる。
        """
        source_name = await self._resolve_source_name(ready.article_id)
        payload = self._success_payload(ready, envelope, source_name)
        await self._events.append(
            stage=Stage.CURATION,
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
        curator: BaseCurator,
    ) -> None:
        """``mark_article_unprocessable`` 内で article DELETE 直前に焼く audit。

        Service が同一 tx で DELETE と組み合わせる (本 class は commit しない)。
        ``code`` は Stage 3 marker の ``code`` instance attr (Layer 2 SSoT、ACL が
        provider ``CODE`` を引き継ぐ)、``category`` は固定で
        ``NON_RETRYABLE_DROP_ARTICLE``。

        失敗経路は envelope を持たない (AI 呼び出し前 or 中の失敗) ため
        ``ai_model`` / ``prompt_version`` は ``curator`` の property
        (``model_name`` / ``prompt_version``) から埋める。

        ``error_chain`` は ``extract_error_chain`` で ``__cause__`` を辿り、ACL の
        ``raise from exc`` で保持された元 ``AIProviderError`` まで記録する。
        """
        source_name = await self._resolve_source_name(article_id)
        payload = CurationPayload(
            **base_curation_payload_fields(
                original_content=original_content,
                source_name=source_name,
            ),
            ai_model=curator.model_name,
            prompt_version=curator.prompt_version,
            # red-team chain γ-2: SDK exception message に key prefix /
            # Authorization header が混入する経路を redact してから永続化する。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            article_id=article_id,
            error_class=_fqn(exc),
            category=Layer1Category.NON_RETRYABLE_DROP_ARTICLE,
            code=code,
        )

    # --- 救済断念経路 (年齢削除と同一 tx) ---------------------------------

    async def append_backfill_curation_aged_out(self, *, article_id: int) -> None:
        """backfill が古い未処理記事を物理削除する直前に焼く監査(commit は caller)。

        意図的な組合せ: ``stage=BACKFILL_CURATE``(curation 救済の保守動作) +
        ``payload.kind=curation``(curation 対象記事の事実)。AI 呼び出しを伴わない
        年齢起因の断念のため envelope / curator / exc は持たない。

        内容拒否の drop(``stage=CURATION`` / ``category=NON_RETRYABLE_DROP_ARTICLE``)
        とは stage / event_type / category / code が全て異なる別経路。
        """
        source_name = await self._resolve_source_name(article_id)
        await self._events.append(
            stage=Stage.BACKFILL_CURATE,
            event_type=EventType.REJECTED,
            outcome_code=BACKFILL_CURATION_AGED_OUT_CODE,
            payload=CurationPayload(source_name=source_name),
            article_id=article_id,
            category=None,  # 明示的に NULL(curation 分類ではない)
            code=BACKFILL_CURATION_AGED_OUT_CODE,
        )

    # --- 失敗経路 (Task 層 4 marker dispatch) -----------------------------

    async def append_failure(
        self,
        *,
        ready: ReadyForCuration,
        exc: BaseException,
        attempt: int,
        curator: BaseCurator,
    ) -> None:
        """CurationTerminalKeepError / CurationRecoverableError / catch-all
        経路の failure audit を 1 行記録する。

        ``category`` / ``code`` は ``exc`` から自動導出 (Stage 3 marker
        isinstance 分岐 + ``exc.code`` instance attr 抽出)。Service と独立に
        Task 層から呼ばれるため別 session (caller が ``tasks.py`` の task 関数
        末尾で開閉 + commit する; PR4 で helper 廃止、task 末尾に inline)。

        失敗経路は envelope を持たない (AI 呼び出し前 or 中の失敗) ため
        ``ai_model`` / ``prompt_version`` は ``curator`` の property
        (``model_name`` / ``prompt_version``) から埋める。

        ``error_chain`` は ``extract_error_chain`` で ``__cause__`` を辿り、ACL の
        ``raise from exc`` で保持された元 ``AIProviderError`` まで記録する。
        """
        source_name = await self._resolve_source_name(ready.article_id)
        payload = CurationPayload(
            **base_curation_payload_fields(
                original_content=ready.original_content,
                source_name=source_name,
            ),
            ai_model=curator.model_name,
            prompt_version=curator.prompt_version,
            # red-team chain γ-2: SDK exception message に key prefix /
            # Authorization header が混入する経路を redact してから永続化する。
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        category = self._category_of(exc)
        code = self._code_of(exc)
        await self._events.append(
            stage=Stage.CURATION,
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
        ready: ReadyForCuration,
        envelope: CurationCall[Signal] | CurationCall[Noise],
        source_name: str | None,
    ) -> CurationPayload:
        """成功経路 audit payload を envelope 経由で組み立てる。

        Stage 4 ``append_in_scope`` / ``append_out_of_scope`` と対称: ``ai_model``
        / ``prompt_version`` / ``raw_relevance`` は envelope から直接読み、Gemini
        ClassVar への静的依存を持たない
        (``feedback_bc_boundary_guarantees_downstream``)。``ai_raw_response`` は
        ``raw_response[:LIMIT]`` で切り詰める。
        """
        return CurationPayload(
            **base_curation_payload_fields(
                original_content=ready.original_content,
                source_name=source_name,
            ),
            ai_model=envelope.model_name,
            prompt_version=envelope.prompt_version,
            ai_raw_response=envelope.raw_response[:_AI_RAW_RESPONSE_LIMIT] or None,
            raw_relevance=envelope.raw_relevance,
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
        """Stage 3 marker / 外部 DB 例外から DB ``category`` 値を導出する。

        自前 marker を先に isinstance 分岐し、非 marker の外部 DB 例外は
        ``classify_db_error`` adapter で分類する (末尾 ``UNKNOWN`` の直前)。
        """
        if isinstance(exc, CurationTerminalDropError):
            return Layer1Category.NON_RETRYABLE_DROP_ARTICLE
        if isinstance(exc, CurationTerminalKeepError):
            return Layer1Category.NON_RETRYABLE_KEEP_ARTICLE
        if isinstance(exc, CurationRecoverableError):
            return Layer1Category.RETRYABLE
        db = classify_db_error(exc)
        if db is not None:
            if db.cause is DbErrorCause.RUNTIME:
                return Layer1Category.RETRYABLE
            if db.cause is DbErrorCause.UNKNOWN:
                return Layer1Category.UNKNOWN
            # CONSTRAINT / QUERY_OR_SCHEMA: DB エラーで記事削除は危険、KEEP が保守的。
            return Layer1Category.NON_RETRYABLE_KEEP_ARTICLE
        return Layer1Category.UNKNOWN

    @staticmethod
    def _code_of(exc: BaseException) -> str:
        """失敗 audit の ``code`` を導出する。

        自前 Stage 3 marker だけ ``.code`` instance attr を信用する (ACL が provider
        ``CODE`` を引き継ぎ、Stage 3 specific は ``code=...`` を pin)。非 marker の
        外部 DB 例外は ``classify_db_error`` adapter で分類し、それ以外は catch-all。
        原則「知らない例外の ``.code`` は読まない」(SQLAlchemy の ``.code=gkpj``
        誤取得を防ぐため ``getattr`` は使わない)。
        """
        if isinstance(
            exc,
            (
                CurationTerminalDropError,
                CurationTerminalKeepError,
                CurationRecoverableError,
            ),
        ):
            return exc.code
        db = classify_db_error(exc)
        if db is not None:
            return db.code
        return "unexpected_error"


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
