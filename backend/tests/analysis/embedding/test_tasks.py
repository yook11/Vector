"""``generate_embedding`` task のテスト (2 marker dispatch + catch-all)。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): generate_embedding は
``EmbeddingTrigger`` (analysis_id のみ) を受領し、task 自身が
``ReadyForEmbedding.try_advance_from`` で Ready を構築する。embedder は
``ctx.state.embedder`` 経由で Pure DI される。

Service.execute は副作用のみで戻り値 ``None`` 一本化 (2026-05-12 確定)。

dispatch 軸:
- precondition 未充足 → svc.execute を呼ばずに return (rate limit も acquire しない)
- ``EmbeddingTerminalSkipError`` → audit + return (taskiq retry なし)
- ``EmbeddingRecoverableError`` → audit + is_last_attempt で raise / return
- catch-all ``Exception`` → audit + is_last_attempt で raise / return
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.analysis.embedding.domain.ready import (
    EmbeddingTrigger,
    ReadyForEmbedding,
)
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalSkipError,
)
from app.analysis.embedding.tasks import _record_failure
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _make_embedder_fake() -> MagicMock:
    """ctx.state.embedder 用のスタブ。MODEL/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.MODEL = "cl-nagoya/ruri-v3-310m"
    fake.RPM = None
    fake.RPD = None
    return fake


def _make_ctx(
    *,
    embedder: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """taskiq Context モック (state.embedder Pure DI)。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=MagicMock())
    if embedder is not None:
        ctx.state.embedder = embedder
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _make_trigger(analysis_id: int = 1) -> EmbeddingTrigger:
    return EmbeddingTrigger(analysis_id=analysis_id)


def _make_ready(analysis_id: int = 1, article_id: int = 7) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis_id,
        text_for_embedding="分析タイトル\n分析要約",
        article_id=article_id,
    )


def _patch_ready_construction(ready: ReadyForEmbedding | None):
    """task 内 ``ReadyForEmbedding.try_advance_from`` を mock する patch。"""
    return patch(
        "app.analysis.embedding.tasks.ReadyForEmbedding.try_advance_from",
        new=AsyncMock(return_value=ready),
    )


# ---------------------------------------------------------------------------
# generate_embedding
# ---------------------------------------------------------------------------


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_task_completes_on_service_success(self) -> None:
        """Service.execute が None を返したら task は完了する。"""
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        trigger = _make_trigger(analysis_id=1)
        ready = _make_ready(analysis_id=1)

        with (
            _patch_ready_construction(ready),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        # 構築された Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready

    @pytest.mark.asyncio
    async def test_skips_when_precondition_not_met(self) -> None:
        """try_advance_from が None を返したら svc.execute を呼ばずに return。

        rate limit acquire も試みない (Ready 構築が gatekeeper、案 3 順序)。
        """
        from app.analysis.embedding.tasks import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        trigger = _make_trigger(analysis_id=42)

        with (
            _patch_ready_construction(None),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
            ) as mock_limiters,
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
        ):
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        # rate limit acquire は試みず、Service も呼ばない
        mock_limiters.assert_not_called()
        mock_svc_cls.assert_not_called()


# ---------------------------------------------------------------------------
# generate_embedding: 2 marker dispatch + catch-all
# ---------------------------------------------------------------------------


class TestGenerateEmbeddingMarkerDispatch:
    """``generate_embedding`` の except 句は 2 marker dispatch
    (TerminalSkip → Recoverable → Exception)。各 except で
    ``_record_failure`` を呼び出す (Stage 4 と同型)。"""

    @pytest.mark.asyncio
    async def test_terminal_skip_records_failure_and_returns(self) -> None:
        """``EmbeddingTerminalSkipError`` → audit + return (taskiq retry なし)。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingTerminalSkipError("bad config", code="ai_error_configuration")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            patch(
                "app.analysis.embedding.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await generate_embedding(trigger=trigger, ctx=ctx)

        mock_audit.assert_awaited_once()
        assert mock_audit.await_args.kwargs["exc"] is exc

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_raises_when_not_last(self) -> None:
        """``EmbeddingRecoverableError`` + retry 余地あり → audit + raise。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            patch(
                "app.analysis.embedding.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(EmbeddingRecoverableError):
                await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_returns_when_last(self) -> None:
        """``EmbeddingRecoverableError`` + 最終 attempt → audit + return。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            patch(
                "app.analysis.embedding.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_response_invalid_dispatches_to_recoverable(self) -> None:
        """``EmbeddingResponseInvalidError`` (Layer 2-B、Recoverable 継承) は
        Recoverable 句に dispatch される (catch-all に落ちない)。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = EmbeddingResponseInvalidError("dimension mismatch")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            patch(
                "app.analysis.embedding.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(EmbeddingResponseInvalidError):
                await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(
            mock_audit.await_args.kwargs["exc"], EmbeddingRecoverableError
        )

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_returns_when_last(self) -> None:
        """任意 ``Exception`` + 最終 attempt → catch-all で audit + return。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            patch(
                "app.analysis.embedding.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(mock_audit.await_args.kwargs["exc"], ValueError)

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_raises_when_not_last(self) -> None:
        """任意 ``Exception`` + retry 余地あり → catch-all で audit + raise。"""
        from app.analysis.embedding.tasks import generate_embedding

        ctx = _make_ctx(embedder=_make_embedder_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.embedding.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.embedding.tasks.EmbeddingService") as mock_svc_cls,
            patch(
                "app.analysis.embedding.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            with pytest.raises(ValueError, match="surprise"):
                await generate_embedding(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# _record_failure 直接呼出: 別 session で audit / 失敗時 log fallback / redact
# ---------------------------------------------------------------------------


async def _make_analysis(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> tuple[InScopeAssessment, int]:
    """Stage 4 完了済みの analysis を 1 件作成し (analysis, article_id) を返す。"""
    article = Article(
        source_id=sample_source.id,
        source_url="https://e.com/a",  # type: ignore[arg-type]
        original_title="t",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="title",
        summary="summary",
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    analysis = InScopeAssessment(
        extraction_id=extraction.id,
        translated_title="title",
        summary="summary",
        investor_take="take",
        ai_model="gemini-2.5-flash-lite",
        topic="topic",
        category_id=sample_categories[0].id,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)
    return analysis, article.id


def _ready_for(analysis: InScopeAssessment, article_id: int) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analysis_id=analysis.id,
        text_for_embedding="title\nsummary",
        article_id=article_id,
    )


class TestRecordFailureHelper:
    """``_record_failure`` private helper の直接テスト (Stage 4 と同形)。

    Task 層 dispatch 経由ではなく helper 単体の挙動を検証する:
    - 正常系: 別 session で 1 行 INSERT (業務 tx と独立)
    - 異常系: session_factory が常に raise → ``embedding_failure_audit_dropped``
      log fallback + business exception を再 raise しない
    - 異常系 redact: business / audit exception の message に混入した
      Authorization Bearer prefix がログ field から除去される (γ-2 対称化)
    """

    @pytest.mark.asyncio
    async def test_records_failure_in_separate_session(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        sample_source: NewsSource,
        sample_categories: list[Category],
    ) -> None:
        """別 session で audit が 1 行 INSERT される (業務 tx と独立)。"""
        analysis, article_id = await _make_analysis(
            db_session, sample_source, sample_categories
        )
        exc = EmbeddingRecoverableError("transient", code="ai_error_network")

        await _record_failure(
            session_factory,
            ready=_ready_for(analysis, article_id),
            exc=exc,
            attempt=2,
        )

        rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "failed"
        assert row.attempt == 2
        assert row.article_id == article_id
        assert row.stage == "embedding"

    @pytest.mark.asyncio
    async def test_audit_insert_failure_logs_and_swallows(self) -> None:
        """``session_factory`` が常に raise する場合、log fallback で観測可能。

        business exception を再 raise しないことも同時に検証する
        (業務 task を audit 失敗で殺さない、best-effort 運用シグナル)。
        """

        class _BoomFactory:
            def __call__(self) -> Any:
                raise RuntimeError("db down")

        ready = ReadyForEmbedding(
            analysis_id=42,
            text_for_embedding="t\ns",
            article_id=7,
        )
        business_exc = EmbeddingRecoverableError("net timeout", code="ai_error_network")

        with capture_logs() as cap:
            await _record_failure(
                _BoomFactory(),  # type: ignore[arg-type]
                ready=ready,
                exc=business_exc,
                attempt=3,
            )

        drops = [e for e in cap if e.get("event") == "embedding_failure_audit_dropped"]
        assert drops, "fallback ログが emit されていない"
        drop = drops[-1]
        assert drop["analysis_id"] == 42
        assert drop["attempt"] == 3
        assert drop["business_error_class"].endswith(".EmbeddingRecoverableError")
        assert drop["business_error_message"] == "net timeout"
        assert drop["audit_error_class"].endswith(".RuntimeError")

    @pytest.mark.asyncio
    async def test_audit_insert_failure_log_redacts_secrets(self) -> None:
        """log fallback の error_message field に secret prefix を漏らさない。

        red-team chain γ-2 対称化: DB payload (``audit_repository.py``) と同様に
        log 経路にも ``redact_secrets`` を通して Authorization Bearer / API key
        prefix がログから消えていることを確認する。
        """

        class _BoomFactory:
            def __call__(self) -> Any:
                # audit_exc 側にも secret を混ぜて両方の redact を検証する
                raise RuntimeError("boom Authorization: Bearer sk-live-AUDITSECRETxyz")

        ready = ReadyForEmbedding(
            analysis_id=99,
            text_for_embedding="t\ns",
            article_id=11,
        )
        # business_exc の message にも secret prefix を仕込む
        business_exc = EmbeddingRecoverableError(
            "upstream failed Authorization: Bearer sk-live-BUSINESSSECRETabc",
            code="ai_error_network",
        )

        with capture_logs() as cap:
            await _record_failure(
                _BoomFactory(),  # type: ignore[arg-type]
                ready=ready,
                exc=business_exc,
                attempt=1,
            )

        drops = [e for e in cap if e.get("event") == "embedding_failure_audit_dropped"]
        assert drops, "fallback ログが emit されていない"
        drop = drops[-1]
        assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
        assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
