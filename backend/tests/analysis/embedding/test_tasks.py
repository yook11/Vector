"""``generate_embedding`` task の分岐テスト。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import AIProviderUsageLimitExhaustedError
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildBlockedCode,
    EmbeddingReadyBuildBlockedError,
    ReadyForEmbedding,
)
from app.analysis.failure_handling import FailureHandlingDecision
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.audit.domain.event import Stage
from app.queue.messages.embedding import EmbeddingTrigger
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result
from tests.logfire._span_helpers import stage_attrs

_METRIC = "vector.embedding.processing_outcome"
_ALL_RESULTS = ("succeeded", "failed", "infra_error")


def _validation_error() -> ValidationError:
    """ready 構築由来の本物の ValidationError を捕捉して返す。"""
    try:
        ReadyForEmbedding(analyzed_article_id=1)  # type: ignore[call-arg]
    except ValidationError as exc:
        return exc
    raise AssertionError("ReadyForEmbedding が ValidationError を出さなかった")


def _make_embedder_fake() -> MagicMock:
    fake = MagicMock()
    fake.model_name = "gemini-embedding-001"
    fake.dimension = 768
    fake.rate_limit_policy = AIModelRateLimitPolicy(
        provider="gemini",
        model="gemini-embedding-001",
        rules=(),
    )
    fake.document_prefix = ""
    return fake


def _make_gate_fake(*, acquired: bool = True) -> MagicMock:
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=acquired)
    return gate


def _make_ctx(
    *,
    embedder: MagicMock | None = None,
    gate: MagicMock | None = None,
    retries: int = 0,
    max_retries: int = 0,
) -> MagicMock:
    ctx = MagicMock()
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=gate if gate is not None else _make_gate_fake(),
    )
    if embedder is not None:
        ctx.state.embedder = embedder
    # taskiq SimpleRetryMiddleware が書く label は "_retries" (0..max_retries-1)
    ctx.message.labels = {
        "_retries": retries,
        "max_retries": max_retries,
    }
    return ctx


def _make_trigger(
    analyzed_article_id: int = 1, analyzable_article_id: int | None = None
) -> EmbeddingTrigger:
    return EmbeddingTrigger(
        analyzed_article_id=analyzed_article_id,
        analyzable_article_id=analyzable_article_id,
    )


def _make_ready(analyzed_article_id: int = 1) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analyzed_article_id=analyzed_article_id,
        text_for_embedding="分析タイトル\n分析要約",
    )


def _patch_ready_construction(
    result: ReadyForEmbedding | EmbeddingReadyBuildBlockedError,
    *,
    analyzable_article_id: int = 7,
):
    # try_advance_from は (ready, analyzable_article_id) を返す。
    mock = (
        AsyncMock(side_effect=result)
        if isinstance(result, EmbeddingReadyBuildBlockedError)
        else AsyncMock(return_value=(result, analyzable_article_id))
    )
    return patch(
        "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
        new=mock,
    )


class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_task_completes_on_service_success(self) -> None:
        """Service.execute が None を返したら task は完了する。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        trigger = _make_trigger(analyzed_article_id=1)
        ready = _make_ready(analyzed_article_id=1)

        with (
            _patch_ready_construction(ready),
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_svc_cls.return_value.execute.assert_called_once()
        # 構築された Ready が Service に渡されていること
        call_args = mock_svc_cls.return_value.execute.call_args
        assert call_args[0][0] is ready
        # gate.acquire は embedder.rate_limit_policy で呼ばれる
        mock_ctx.state.provider_rate_limit_gate.acquire.assert_awaited_once_with(
            mock_ctx.state.embedder.rate_limit_policy
        )

    @pytest.mark.asyncio
    async def test_passes_trigger_analyzable_id_as_hint(self) -> None:
        """新 message では trigger.analyzable_article_id を hint に渡す。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        trigger = _make_trigger(analyzed_article_id=1, analyzable_article_id=99)
        advance = AsyncMock(return_value=(_make_ready(analyzed_article_id=1), 99))
        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=advance,
            ),
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        assert advance.await_args.kwargs["analyzable_hint"] == 99

    @pytest.mark.asyncio
    async def test_passes_none_hint_for_legacy_trigger(self) -> None:
        """analyzable_article_id 未設定の旧 message では hint=None を渡す。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        trigger = _make_trigger(analyzed_article_id=1)
        advance = AsyncMock(return_value=(_make_ready(analyzed_article_id=1), 7))
        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=advance,
            ),
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        assert advance.await_args.kwargs["analyzable_hint"] is None

    @pytest.mark.asyncio
    async def test_ready_build_blocked_audits_and_does_not_call_service(self) -> None:
        """Ready build blocked なら rejected audit + return、Service は呼ばない。

        rate limit acquire も試みない (Ready 構築が gatekeeper)。
        """
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake()
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        trigger = _make_trigger(analyzed_article_id=42)
        exc = EmbeddingReadyBuildBlockedError(
            EmbeddingReadyBuildBlockedCode.ANALYZED_ARTICLE_MISSING
        )

        with (
            _patch_ready_construction(exc),
            patch("app.queue.tasks.embedding.EmbeddingAuditRepository") as mock_audit,
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        mock_audit.return_value.append_ready_build_blocked.assert_awaited_once_with(
            analyzed_article_id=42,
            exc=exc,
        )
        # rate limit acquire は試みず、Service も呼ばない
        gate.acquire.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_skip_escapes_audit_and_logs_only(self) -> None:
        """ALREADY_EMBEDDED (冪等 skip) は監査行を焼かず log のみに逃がす。"""
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake()
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        trigger = _make_trigger(analyzed_article_id=42)
        exc = EmbeddingReadyBuildBlockedError(
            EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED
        )

        with (
            _patch_ready_construction(exc),
            patch("app.queue.tasks.embedding.EmbeddingAuditRepository") as mock_audit,
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
            capture_logs() as cap,
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        # 冪等 skip は repository を触らず pipeline_events 行を焼かない
        mock_audit.assert_not_called()
        rejected = [e for e in cap if e["event"] == "generate_embedding_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["code"] == exc.code.value
        gate.acquire.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_ready_build_exception_audits_and_reraises(self) -> None:
        """Ready 判定中の例外は failed audit 後に元例外を raise する。"""
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake()
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        trigger = _make_trigger(analyzed_article_id=42)
        exc = RuntimeError("ready build exploded")

        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(side_effect=exc),
            ),
            patch(
                "app.queue.tasks.embedding._append_ready_build_failed_audit",
                new=AsyncMock(),
            ) as audit_failed,
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            with pytest.raises(RuntimeError):
                await generate_embedding(trigger=trigger, ctx=mock_ctx)

        audit_failed.assert_awaited_once_with(
            mock_ctx.state.session_factory,
            analyzed_article_id=42,
            exc=exc,
        )
        gate.acquire.assert_not_called()
        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_gate_denies_quota(self) -> None:
        """gate.acquire が False なら svc を呼ばず gate skip の log + metric を出す。"""
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake(acquired=False)
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        trigger = _make_trigger(analyzed_article_id=1)
        ready = _make_ready(analyzed_article_id=1)

        with (
            _patch_ready_construction(ready),
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
            patch(
                "app.queue.tasks.embedding.record_rate_limit_gate_skipped"
            ) as mock_record,
            capture_logs() as cap,
        ):
            await generate_embedding(trigger=trigger, ctx=mock_ctx)

        gate.acquire.assert_awaited_once()
        mock_svc_cls.assert_not_called()
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["stage"] is Stage.EMBEDDING
        assert mock_record.call_args.kwargs["model"] == "gemini-embedding-001"
        skips = [
            e for e in cap if e.get("event") == "embedding_ai_rate_limit_gate_skipped"
        ]
        assert skips, "gate skip log が emit されていない"
        assert skips[-1]["analyzed_article_id"] == 1
        assert skips[-1]["analyzable_article_id"] == 7
        assert skips[-1]["embedding_model"] == "gemini-embedding-001"


class TestGenerateEmbeddingStageSpan:
    """``article_stage`` span の embedding task 配線 (capfire oracle)。

    終端ステージなので next_task 系 attribute は決して出ない。Service は mock する
    ため succeeded の result は service テストが正本。ここでは task が設定する
    skipped / rate_limited / failed、article_id late-binding、終端性を固定する。
    """

    @pytest.mark.asyncio
    async def test_success_has_no_next_task_and_binds_article_id(
        self, capfire: CaptureLogfire
    ) -> None:
        """成功経路は next_task 系を出さず (終端) article_id を late-bind する。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        with (
            _patch_ready_construction(
                _make_ready(analyzed_article_id=1)
            ),  # article_id=7
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            await generate_embedding(
                trigger=_make_trigger(analyzed_article_id=1), ctx=mock_ctx
            )

        attrs = stage_attrs(capfire)
        assert attrs["article_id"] == 7
        assert "next_task_enqueued" not in attrs
        assert "next_task_name" not in attrs
        # result は service (mock) の責務。task は success 経路で result を設定しない。
        assert "result" not in attrs

    @pytest.mark.asyncio
    async def test_ready_build_blocked_sets_skipped(
        self, capfire: CaptureLogfire
    ) -> None:
        """Ready build blocked 経路で task が skipped を焼く (article_id 無し)。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        exc = EmbeddingReadyBuildBlockedError(
            EmbeddingReadyBuildBlockedCode.ANALYZED_ARTICLE_MISSING
        )
        with (
            _patch_ready_construction(exc),
            patch("app.queue.tasks.embedding.EmbeddingAuditRepository") as mock_audit,
            patch("app.queue.tasks.embedding.EmbeddingService"),
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            await generate_embedding(
                trigger=_make_trigger(analyzed_article_id=42), ctx=mock_ctx
            )

        attrs = stage_attrs(capfire)
        assert attrs["result"] == "skipped"
        assert "article_id" not in attrs

    @pytest.mark.asyncio
    async def test_ready_build_exception_sets_failed_via_backstop(
        self, capfire: CaptureLogfire
    ) -> None:
        """Ready 構築例外 (task は result 不設定) → backstop が failed を焼く。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "app.queue.tasks.embedding._append_ready_build_failed_audit",
                new=AsyncMock(),
            ),
            patch("app.queue.tasks.embedding.EmbeddingService"),
        ):
            with pytest.raises(RuntimeError):
                await generate_embedding(
                    trigger=_make_trigger(analyzed_article_id=42), ctx=mock_ctx
                )

        assert stage_attrs(capfire)["result"] == "failed"

    @pytest.mark.asyncio
    async def test_gate_skip_sets_rate_limited(self, capfire: CaptureLogfire) -> None:
        """gate.acquire=False 経路で task が rate_limited を焼く。"""
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake(acquired=False)
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        with (
            _patch_ready_construction(_make_ready(analyzed_article_id=1)),
            patch("app.queue.tasks.embedding.EmbeddingService"),
            patch("app.queue.tasks.embedding.record_rate_limit_gate_skipped"),
        ):
            await generate_embedding(
                trigger=_make_trigger(analyzed_article_id=1), ctx=mock_ctx
            )

        assert stage_attrs(capfire)["result"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_terminal_sets_failure_attrs_without_drop_article(
        self, capfire: CaptureLogfire
    ) -> None:
        """Service が terminal marker を raise したとき span に failure 属性が焼かれる。

        期待値の根拠:
        - AIProviderUsageLimitExhaustedError: CODE="ai_error_usage_limit_exhausted",
          FAILURE_MODE=CONDITION_BASED_RECOVERY (ai_provider_errors.py)
        - to_embedding_error: CONDITION_BASED_RECOVERY.retryable=True
          (ai_provider_errors.py) → EmbeddingRecoverableError,
          failure_kind="condition_based_recovery", code=exc.CODE (embedding/errors.py)
        - EmbeddingRecoverableError: RETRYABILITY=RETRYABLE, FAILURE_ACTION=None
          → failure_action は span に載らない (failure_attrs.py: None なら set しない)
        """
        from app.analysis.embedding.errors import to_embedding_error
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        raw = AIProviderUsageLimitExhaustedError()
        marker = to_embedding_error(raw)

        with (
            _patch_ready_construction(_make_ready(analyzed_article_id=1)),
            patch("app.queue.tasks.embedding.EmbeddingService") as mock_svc_cls,
            patch(
                "app.queue.tasks.embedding.EmbeddingFailureHandler"
            ) as mock_handler_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=marker)
            mock_handler_cls.return_value.handle = AsyncMock(
                return_value=FailureHandlingDecision(reraise=False)
            )
            await generate_embedding(
                trigger=_make_trigger(analyzed_article_id=1), ctx=mock_ctx
            )

        attrs = stage_attrs(capfire)
        assert attrs["result"] == "failed"
        # failure_kind: CONDITION_BASED_RECOVERY.value (embedding/errors.py)
        assert attrs["failure_kind"] == "condition_based_recovery"
        # code: AIProviderUsageLimitExhaustedError.CODE (ai_provider_errors.py)
        assert attrs["code"] == "ai_error_usage_limit_exhausted"
        # retryability: EmbeddingRecoverableError.RETRYABILITY (embedding/errors.py)
        # CONDITION_BASED_RECOVERY.retryable=True → EmbeddingRecoverableError
        assert attrs["retryability"] == "retryable"
        # error_class: exception_fqn of the marker instance (EmbeddingRecoverableError)
        assert attrs["error_class"].endswith(".EmbeddingRecoverableError")
        # failure_action: FAILURE_ACTION=None → attribute not set (failure_attrs.py)
        assert "failure_action" not in attrs


class TestGenerateEmbeddingProcessingOutcome:
    """ready-build 境界の ``processing_outcome`` 分類 (audit drop 非依存)。"""

    @pytest.mark.asyncio
    async def test_ready_build_db_error_emits_infra_error(
        self, capfire: CaptureLogfire
    ) -> None:
        """ready-build の SQLAlchemyError は infra_error を emit して raise する。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(side_effect=SQLAlchemyError("db down")),
            ),
            patch(
                "app.queue.tasks.embedding._append_ready_build_failed_audit",
                new=AsyncMock(),
            ),
            patch("app.queue.tasks.embedding.EmbeddingService"),
        ):
            with pytest.raises(SQLAlchemyError):
                await generate_embedding(
                    trigger=_make_trigger(analyzed_article_id=1), ctx=mock_ctx
                )

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _METRIC, "infra_error") == 1
        for other in ("succeeded", "failed"):
            assert sum_counter_for_result(metrics, _METRIC, other) == 0

    @pytest.mark.asyncio
    async def test_ready_build_validation_error_emits_failed(
        self, capfire: CaptureLogfire
    ) -> None:
        """ready-build の ValidationError は failed (分母に算入) を emit して raise。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(side_effect=_validation_error()),
            ),
            patch(
                "app.queue.tasks.embedding._append_ready_build_failed_audit",
                new=AsyncMock(),
            ),
            patch("app.queue.tasks.embedding.EmbeddingService"),
        ):
            with pytest.raises(ValidationError):
                await generate_embedding(
                    trigger=_make_trigger(analyzed_article_id=1), ctx=mock_ctx
                )

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _METRIC, "failed") == 1
        for other in ("succeeded", "infra_error"):
            assert sum_counter_for_result(metrics, _METRIC, other) == 0

    @pytest.mark.asyncio
    async def test_ready_build_unexpected_error_emits_failed(
        self, capfire: CaptureLogfire
    ) -> None:
        """ready-build の想定外例外は failed を emit して raise (分母から隠さない)。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        with (
            patch(
                "app.queue.tasks.embedding.ReadyForEmbedding.try_advance_from",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "app.queue.tasks.embedding._append_ready_build_failed_audit",
                new=AsyncMock(),
            ),
            patch("app.queue.tasks.embedding.EmbeddingService"),
        ):
            with pytest.raises(RuntimeError):
                await generate_embedding(
                    trigger=_make_trigger(analyzed_article_id=1), ctx=mock_ctx
                )

        metrics = collected_metrics(capfire)
        assert sum_counter_for_result(metrics, _METRIC, "failed") == 1
        for other in ("succeeded", "infra_error"):
            assert sum_counter_for_result(metrics, _METRIC, other) == 0

    @pytest.mark.asyncio
    async def test_gate_skip_does_not_emit_processing_outcome(
        self, capfire: CaptureLogfire
    ) -> None:
        """gate skip (capacity 制御) は処理試行に入らず counter を汚さない。"""
        from app.queue.tasks.embedding import generate_embedding

        gate = _make_gate_fake(acquired=False)
        mock_ctx = _make_ctx(embedder=_make_embedder_fake(), gate=gate)
        with (
            _patch_ready_construction(_make_ready(analyzed_article_id=1)),
            patch("app.queue.tasks.embedding.EmbeddingService"),
            patch("app.queue.tasks.embedding.record_rate_limit_gate_skipped"),
        ):
            await generate_embedding(
                trigger=_make_trigger(analyzed_article_id=1), ctx=mock_ctx
            )

        metrics = collected_metrics(capfire)
        for result in _ALL_RESULTS:
            assert sum_counter_for_result(metrics, _METRIC, result) == 0

    @pytest.mark.asyncio
    async def test_ready_build_blocked_does_not_emit_processing_outcome(
        self, capfire: CaptureLogfire
    ) -> None:
        """ready-build blocked (stale/冪等) は counter を汚さない。"""
        from app.queue.tasks.embedding import generate_embedding

        mock_ctx = _make_ctx(embedder=_make_embedder_fake())
        exc = EmbeddingReadyBuildBlockedError(
            EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED
        )
        with (
            _patch_ready_construction(exc),
            patch("app.queue.tasks.embedding.EmbeddingAuditRepository") as mock_audit,
            patch("app.queue.tasks.embedding.EmbeddingService"),
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            await generate_embedding(
                trigger=_make_trigger(analyzed_article_id=42), ctx=mock_ctx
            )

        metrics = collected_metrics(capfire)
        for result in _ALL_RESULTS:
            assert sum_counter_for_result(metrics, _METRIC, result) == 0
