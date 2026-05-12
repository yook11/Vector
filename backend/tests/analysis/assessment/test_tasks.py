"""``assess_content`` task のテスト (chain 経路 + 3 marker dispatch)。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): assess_content は
``AssessmentTrigger`` (extraction_id のみ) を受領し、task 自身が
``ReadyForAssessment.try_advance_from`` で Ready を構築する。

- precondition 未充足 → svc.execute を呼ばずに return (rate limit も acquire しない)
- in-scope 成功 (int 返却) → EmbeddingTrigger で embedding chain (ID のみ運ぶ)
- out-of-scope / race lost (``None`` 返却) は chain しないこと
- 3 marker dispatch: TerminalSkip / Recoverable / catch-all Exception
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.analysis.assessment.domain.ready import (
    AssessmentTrigger,
    ReadyForAssessment,
)
from app.analysis.assessment.errors import (
    AssessmentCategoryMissingError,
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalSkipError,
)
from app.analysis.assessment.tasks import _record_failure
from app.analysis.embedding.domain.ready import EmbeddingTrigger
from app.analysis.errors import RateLimitError
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _make_provider_fake() -> MagicMock:
    """assessor 用のスタブ。MODEL/RPM/RPD を持つ。"""
    fake = MagicMock()
    fake.MODEL = "test-model"
    fake.RPM = 50
    fake.RPD = 1500
    return fake


def _make_ctx(
    *,
    assessor: MagicMock | None = None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    """taskiq Context モック。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=MagicMock())
    if assessor is not None:
        ctx.state.assessor = assessor
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _make_trigger(extraction_id: int = 2) -> AssessmentTrigger:
    return AssessmentTrigger(extraction_id=extraction_id)


def _make_ready(extraction_id: int = 2) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction_id,
        translated_title="title",
        summary="summary",
        article_id=7,
        source_name="Test Source",
    )


def _patch_ready_construction(ready: ReadyForAssessment | None):
    """task 内 ``ReadyForAssessment.try_advance_from`` を mock する patch。"""
    return patch(
        "app.analysis.assessment.tasks.ReadyForAssessment.try_advance_from",
        new=AsyncMock(return_value=ready),
    )


# ---------------------------------------------------------------------------
# assess_content (Stage 4)
# ---------------------------------------------------------------------------


class TestAssessContent:
    @pytest.mark.asyncio
    async def test_skips_when_precondition_not_met(self) -> None:
        """try_advance_from が None を返したら svc.execute を呼ばずに return。

        rate limit acquire も試みない (Ready 構築が gatekeeper、案 3 順序)。
        """
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(extraction_id=42)

        with (
            _patch_ready_construction(None),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
            ) as mock_limiters,
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
        ):
            await assess_content(trigger=trigger, ctx=ctx)

        # rate limit acquire は試みず、Service も呼ばない
        mock_limiters.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_scope_chains_embedding_with_trigger(self) -> None:
        """in-scope 成功 (assessment_id 返却) → EmbeddingTrigger で chain。

        案 3: 上流 Stage 4 task は Stage 5 Ready を構築せず、ID だけ運ぶ
        EmbeddingTrigger を kiq に enqueue する。Ready 構築は下流 Stage 5
        task が処理開始時に行う。
        """
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(extraction_id=2)
        ready = _make_ready(extraction_id=2)

        with (
            _patch_ready_construction(ready),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch("app.analysis.assessment.tasks.generate_embedding") as mock_embed,
        ):
            # Service は in-scope 成功時 assessment id を返す
            mock_svc_cls.return_value.execute = AsyncMock(return_value=100)
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=trigger, ctx=mock_ctx)

        # 構築された Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready
        mock_embed.kiq.assert_awaited_once_with(EmbeddingTrigger(analysis_id=100))

    @pytest.mark.asyncio
    async def test_none_result_does_not_chain(self) -> None:
        """``execute`` が None (out-of-scope / race lost) → embedding chain しない。

        in-scope 経路だけが Stage 5 chain の対象。out-of-scope はパイプライン終了で、
        race lost は勝者 task が自身で chain を起動する責務。
        """
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(assessor=_make_provider_fake())
        trigger = _make_trigger(extraction_id=2)

        with (
            _patch_ready_construction(_make_ready(extraction_id=2)),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch("app.analysis.assessment.tasks.generate_embedding") as mock_embed,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_embed.kiq = AsyncMock()
            await assess_content(trigger=trigger, ctx=mock_ctx)

        mock_embed.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_for_retry(self) -> None:
        """legacy RateLimitError は 3 marker いずれにも該当しないので
        catch-all 句に dispatch される。retry 余地ありで raise。"""
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(
            assessor=_make_provider_fake(), retry_count=0, max_retries=2
        )
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ),
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            with pytest.raises(RateLimitError):
                await assess_content(trigger=trigger, ctx=mock_ctx)

    @pytest.mark.asyncio
    async def test_rate_limit_last_attempt_returns(self) -> None:
        """assess_content は最終試行で例外を送出せず return する (catch-all 句)。"""
        from app.analysis.assessment.tasks import assess_content

        mock_ctx = _make_ctx(
            assessor=_make_provider_fake(), retry_count=2, max_retries=2
        )
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ),
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=RateLimitError("429"),
            )
            await assess_content(trigger=trigger, ctx=mock_ctx)


# ---------------------------------------------------------------------------
# assess_content: 3 marker dispatch (TerminalSkip / Recoverable / Exception)
# ---------------------------------------------------------------------------


class TestAssessContentMarkerDispatch:
    """``assess_content`` の except 句は 3 marker dispatch
    (TerminalSkip → Recoverable → Exception)。各 except で
    ``_record_failure`` を呼び出す。"""

    @pytest.mark.asyncio
    async def test_terminal_skip_records_failure_and_returns(self) -> None:
        """``AssessmentTerminalSkipError`` → audit + return (taskiq retry なし)。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentTerminalSkipError("bad config", code="ai_error_configuration")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await assess_content(trigger=trigger, ctx=ctx)

        mock_audit.assert_awaited_once()
        assert mock_audit.await_args.kwargs["exc"] is exc

    @pytest.mark.asyncio
    async def test_category_missing_dispatches_to_terminal_skip(self) -> None:
        """``AssessmentCategoryMissingError`` (Layer 2-B、TerminalSkip 継承) は
        TerminalSkip 句に dispatch される (Recoverable 句に誤って落ちない)。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentCategoryMissingError("unknown slug 'foo'")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            # TerminalSkip 句は raise しない → return 成立
            await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(
            mock_audit.await_args.kwargs["exc"], AssessmentTerminalSkipError
        )

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_raises_when_not_last(self) -> None:
        """``AssessmentRecoverableError`` + retry 余地あり → audit + raise。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(AssessmentRecoverableError):
                await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recoverable_records_failure_and_returns_when_last(self) -> None:
        """``AssessmentRecoverableError`` + 最終 attempt → audit + return。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentRecoverableError("network", code="ai_error_network")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_response_invalid_dispatches_to_recoverable(self) -> None:
        """``AssessmentResponseInvalidError`` (Layer 2-B、Recoverable 継承) は
        Recoverable 句に dispatch される (catch-all に落ちない)。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()
        exc = AssessmentResponseInvalidError("schema violation")

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc)
            with pytest.raises(AssessmentResponseInvalidError):
                await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(
            mock_audit.await_args.kwargs["exc"], AssessmentRecoverableError
        )

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_returns_when_last(self) -> None:
        """任意 ``Exception`` + 最終 attempt → catch-all で audit + return。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=2, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()
        assert isinstance(mock_audit.await_args.kwargs["exc"], ValueError)

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_and_raises_when_not_last(self) -> None:
        """任意 ``Exception`` + retry 余地あり → catch-all で audit + raise。"""
        from app.analysis.assessment.tasks import assess_content

        ctx = _make_ctx(assessor=_make_provider_fake(), retry_count=0, max_retries=2)
        trigger = _make_trigger()

        with (
            _patch_ready_construction(_make_ready()),
            patch(
                "app.analysis.assessment.tasks._build_limiters",
                return_value=(None, None),
            ),
            patch("app.analysis.assessment.tasks.AssessmentService") as mock_svc_cls,
            patch(
                "app.analysis.assessment.tasks._record_failure",
                new=AsyncMock(),
            ) as mock_audit,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(
                side_effect=ValueError("surprise")
            )
            with pytest.raises(ValueError, match="surprise"):
                await assess_content(trigger=trigger, ctx=ctx)
        mock_audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# _record_failure 直接呼出: 別 session で audit / 失敗時 log fallback / redact
# ---------------------------------------------------------------------------


async def _make_extraction(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> ArticleExtraction:
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
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


def _ready_for(
    extraction: ArticleExtraction, *, source_name: str | None = None
) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction.id,
        translated_title=extraction.translated_title,
        summary=extraction.summary,
        article_id=extraction.article_id,
        source_name=source_name,
    )


class TestRecordFailureHelper:
    """``_record_failure`` private helper の直接テスト。

    Task 層 dispatch 経由ではなく helper 単体の挙動を検証する:
    - 正常系: 別 session で 1 行 INSERT (業務 tx と独立)
    - 異常系: session_factory が常に raise → ``assessment_failure_audit_dropped``
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
    ) -> None:
        """別 session で audit が 1 行 INSERT される (業務 tx と独立)。"""
        extraction = await _make_extraction(db_session, sample_source)
        exc = AssessmentRecoverableError("transient", code="ai_error_network")

        await _record_failure(
            session_factory,
            ready=_ready_for(extraction),
            exc=exc,
            attempt=2,
        )

        rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "failed"
        assert row.attempt == 2
        assert row.payload["extraction_id"] == extraction.id

    @pytest.mark.asyncio
    async def test_audit_insert_failure_logs_and_swallows(self) -> None:
        """``session_factory`` が常に raise する場合、log fallback で観測可能。

        business exception を再 raise しないことも同時に検証する
        (業務 task を audit 失敗で殺さない、best-effort 運用シグナル)。
        """

        class _BoomFactory:
            def __call__(self) -> Any:
                raise RuntimeError("db down")

        ready = ReadyForAssessment(
            extraction_id=42,
            translated_title="t",
            summary="s",
            article_id=7,
            source_name=None,
        )
        business_exc = AssessmentRecoverableError(
            "net timeout", code="ai_error_network"
        )

        with capture_logs() as cap:
            await _record_failure(
                _BoomFactory(),  # type: ignore[arg-type]
                ready=ready,
                exc=business_exc,
                attempt=3,
            )

        drops = [e for e in cap if e.get("event") == "assessment_failure_audit_dropped"]
        assert drops, "fallback ログが emit されていない"
        drop = drops[-1]
        assert drop["extraction_id"] == 42
        assert drop["attempt"] == 3
        assert drop["business_error_class"].endswith(".AssessmentRecoverableError")
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

        ready = ReadyForAssessment(
            extraction_id=99,
            translated_title="t",
            summary="s",
            article_id=11,
            source_name=None,
        )
        # business_exc の message にも secret prefix を仕込む
        business_exc = AssessmentRecoverableError(
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

        drops = [e for e in cap if e.get("event") == "assessment_failure_audit_dropped"]
        assert drops, "fallback ログが emit されていない"
        drop = drops[-1]
        assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
        assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
