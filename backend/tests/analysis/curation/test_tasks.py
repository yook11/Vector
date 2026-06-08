"""``curate_content`` task の分岐テスト。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderRateLimitedError,
)
from app.analysis.curation.domain.ready import (
    CurationReadyBuildBlockedCode,
    CurationReadyBuildBlockedError,
    ReadyForCuration,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.curation import CurationTrigger
from tests.logfire._span_helpers import stage_attrs


def _make_provider_fake() -> MagicMock:
    fake = MagicMock()
    fake.model_name = "test-model"
    fake.prompt_version = "test-prompt-v1"
    fake.rate_limit_policy = AIModelRateLimitPolicy(
        provider="gemini",
        model="test-model",
        rules=(
            RateLimitRule(
                name="rpd", max_requests=1500, window_seconds=86400, block=False
            ),
            RateLimitRule(name="rpm", max_requests=50, window_seconds=60, block=True),
        ),
    )
    return fake


def _make_ctx(
    *,
    curator: MagicMock | None = None,
    retries: int = 0,
    max_retries: int = 0,
    gate_acquire: bool = True,
) -> MagicMock:
    ctx = MagicMock()
    gate = MagicMock()
    gate.acquire = AsyncMock(return_value=gate_acquire)
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        provider_rate_limit_gate=gate,
    )
    if curator is not None:
        ctx.state.curator = curator
    # taskiq SimpleRetryMiddleware が書く label は "_retries" (0..max_retries-1)
    ctx.message.labels = {
        "_retries": retries,
        "max_retries": max_retries,
    }
    return ctx


def _trigger(article_id: int = 1) -> CurationTrigger:
    return CurationTrigger(article_id=article_id)


def _fixed_ready(article_id: int = 1) -> ReadyForCuration:
    return ReadyForCuration(
        article_id=article_id,
        original_title="Title",
        original_content="content",
    )


def _patch_try_advance_from(
    result: ReadyForCuration | CurationReadyBuildBlockedError,
) -> object:
    mock = (
        AsyncMock(side_effect=result)
        if isinstance(result, CurationReadyBuildBlockedError)
        else AsyncMock(return_value=result)
    )
    return patch.object(
        ReadyForCuration,
        "try_advance_from",
        new=mock,
    )


# curate_content


class TestCurateContent:
    @pytest.mark.asyncio
    async def test_chains_assess_with_trigger_when_service_returns_curation_id(
        self,
    ) -> None:
        """signal 勝者 (Service が int を返す) → ``assess_content.kiq`` で chain。

        案 3: 上流 Stage 3 task は Stage 4 Ready を構築せず、ID だけ運ぶ
        AssessmentTrigger を kiq に enqueue する。Ready 構築は下流 Stage 4
        task が処理開始時に行う。
        """
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())

        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=42)
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        mock_assess.kiq.assert_awaited_once_with(
            AssessmentTrigger(curation_id=42),
        )

    @pytest.mark.asyncio
    async def test_noise_or_race_loss_does_not_chain(self) -> None:
        """Service が None を返したら chain しない (noise 勝者 / race 敗北を吸収)。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())

        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_ready_build_blocked_audits_and_does_not_call_service(self) -> None:
        """Ready build blocked なら rejected audit + return、Service は呼ばない。

        案 3: precondition (article 既消滅 / 既処理 / 本文 oversized) の
        判定は Stage 3 task 冒頭で Ready 自構築時に行われ、未充足なら
        AI quota / Service を消費せず短絡する。
        """
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())
        exc = CurationReadyBuildBlockedError(
            CurationReadyBuildBlockedCode.ARTICLE_MISSING
        )

        with (
            _patch_try_advance_from(exc),
            patch("app.queue.tasks.curation.CurationAuditRepository") as mock_audit,
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        mock_audit.return_value.append_ready_build_blocked.assert_awaited_once_with(
            target_article_id=1,
            exc=exc,
        )
        # Service / rate limit gate / chain firing いずれも触らない
        mock_svc_cls.assert_not_called()
        mock_ctx.state.provider_rate_limit_gate.acquire.assert_not_called()
        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_ready_build_exception_audits_and_reraises(self) -> None:
        """Ready 判定中の例外は failed audit 後に元例外を raise する。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())
        exc = RuntimeError("ready build exploded")

        with (
            patch.object(
                ReadyForCuration,
                "try_advance_from",
                new=AsyncMock(side_effect=exc),
            ),
            patch(
                "app.queue.tasks.curation._append_ready_build_failed_audit",
                new=AsyncMock(),
            ) as audit_failed,
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_assess.kiq = AsyncMock()
            with pytest.raises(RuntimeError):
                await curate_content(trigger=_trigger(), ctx=mock_ctx)

        audit_failed.assert_awaited_once_with(
            mock_ctx.state.session_factory,
            article_id=1,
            exc=exc,
        )
        mock_svc_cls.assert_not_called()
        mock_ctx.state.provider_rate_limit_gate.acquire.assert_not_called()
        mock_assess.kiq.assert_not_called()

    @pytest.mark.asyncio
    async def test_quota_skip_returns_without_invoking_service(self) -> None:
        """gate.acquire=False の場合 gate skip の log + metric を出して return する。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake(), gate_acquire=False)

        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
            patch(
                "app.queue.tasks.curation.record_rate_limit_gate_skipped"
            ) as mock_record,
            capture_logs() as cap,
        ):
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        mock_svc_cls.assert_not_called()
        mock_assess.kiq.assert_not_called()
        mock_record.assert_called_once_with(stage="curation", model="test-model")
        skips = [
            e for e in cap if e.get("event") == "curation_ai_rate_limit_gate_skipped"
        ]
        assert skips, "gate skip log が emit されていない"
        assert skips[-1]["ai_model"] == "test-model"
        assert skips[-1]["prompt_version"] == "test-prompt-v1"

    @pytest.mark.asyncio
    async def test_rate_limited_records_audit_and_returns(self) -> None:
        """RateLimited は CurationRecoverableError に詰め替えられる経路。

        本番経路 (Service.execute) で ACL ``map_provider_to_curation`` により
        Stage 3 marker に詰め替えられる。本テストは Service を mock しているため、
        production と同じ詰め替え済 marker を side_effect として渡して
        handler の挙動 (last_attempt → audit + return) を再現する。
        """
        from app.analysis.curation.errors import map_provider_to_curation
        from app.queue.tasks.curation import curate_content

        # 最終試行: _retries=max_retries-1=1 (旧 max_retries=1 は非最終が存在しない)
        mock_ctx = _make_ctx(curator=_make_provider_fake(), retries=1, max_retries=2)
        raw_exc = AIProviderRateLimitedError("429")
        try:
            raise map_provider_to_curation(raw_exc) from raw_exc
        except Exception as wrapped:  # noqa: BLE001
            wrapped_exc = wrapped

        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch(
                "app.analysis.curation.failure_handling.CurationAuditRepository"
            ) as mock_audit_cls,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=wrapped_exc)
            mock_audit_cls.return_value.append_failure = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)
        mock_audit_cls.return_value.append_failure.assert_awaited_once()
        # 詰め替え済 Stage 3 marker が audit に渡る (元 provider は __cause__)。
        audit_exc = mock_audit_cls.return_value.append_failure.await_args.kwargs["exc"]
        assert audit_exc is wrapped_exc
        assert isinstance(audit_exc.__cause__, AIProviderRateLimitedError)

    @pytest.mark.asyncio
    async def test_audit_failure_falls_back_to_log(self) -> None:
        """audit Repository が raise しても task は落ちず log fallback する。

        PR4 で ``_record_failure`` helper を廃止し task 末尾の inline audit に
        統一したため、helper 単体テストの代わりに「audit DB が落ちても業務
        task は完走し ``curation_failure_audit_dropped`` 構造ログが出る」
        振る舞いを task 経由で検証する。同時に business / audit exception の
        message に混入した secret prefix が log field から除去されることも
        確認する (red-team chain γ-2 対称化)。
        """
        from app.analysis.curation.errors import map_provider_to_curation
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake(), retries=0, max_retries=2)
        # Phase 4: AIProviderConfigurationError は accept-and-discard。message
        # 引数は __str__ に出ない (SAFE_ATTRS=("CODE",) 経路のみ) ため、business
        # 側の secret 混入経路は構造的に塞がれる。本テストでは audit 側の
        # redact_secrets 経路を主軸に検証する。
        raw_exc = AIProviderConfigurationError(
            "api key missing Authorization: Bearer sk-live-BUSINESSSECRETabc"
        )
        try:
            raise map_provider_to_curation(raw_exc) from raw_exc
        except Exception as wrapped:  # noqa: BLE001
            business_exc = wrapped

        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch(
                "app.analysis.curation.failure_handling.CurationAuditRepository"
            ) as mock_audit_cls,
            patch("app.queue.tasks.curation.set_curation_hold", new=AsyncMock()),
            capture_logs() as cap,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(side_effect=business_exc)
            mock_audit_cls.return_value.append_failure = AsyncMock(
                side_effect=RuntimeError(
                    "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
                )
            )
            # task は落ちずに完走する
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        drops = [e for e in cap if e.get("event") == "curation_failure_audit_dropped"]
        assert drops, "fallback ログが emit されていない"
        drop = drops[-1]
        assert drop["article_id"] == 1
        assert drop["business_error_class"].endswith(".CurationTerminalKeepError")
        assert drop["audit_error_class"].endswith(".RuntimeError")
        # red-team chain γ-2: business / audit 両方の secret が redact される
        assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
        assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]


class TestCurateContentStageSpan:
    """``article_stage`` span の curation task 配線 (capfire oracle)。

    Service は mock するため signal / noise の result は service テストが正本。
    ここでは task が設定する skipped / rate_limited / failed と、kiq 成功後の
    mark、success 経路で task が result を設定しないことを固定する。
    """

    @pytest.mark.asyncio
    async def test_chain_marks_next_task_without_setting_result(
        self, capfire: CaptureLogfire
    ) -> None:
        """signal 勝者 (kiq 成功) → next_task_enqueued=True + name。result は不設定。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())
        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=42)
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        attrs = stage_attrs(capfire)
        assert attrs["next_task_enqueued"] is True
        assert attrs["next_task_name"] == "assess_content"
        # result は service (mock) の責務。task は success 経路で result を設定しない。
        assert "result" not in attrs

    @pytest.mark.asyncio
    async def test_none_result_does_not_mark_next_task(
        self, capfire: CaptureLogfire
    ) -> None:
        """Service が None (noise / race) → mark せず enqueued は False のまま。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())
        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService") as mock_svc_cls,
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_svc_cls.return_value.execute = AsyncMock(return_value=None)
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        attrs = stage_attrs(capfire)
        assert attrs["next_task_enqueued"] is False
        assert "next_task_name" not in attrs

    @pytest.mark.asyncio
    async def test_ready_build_blocked_sets_skipped(
        self, capfire: CaptureLogfire
    ) -> None:
        """Ready build blocked 経路で task が result=skipped を焼く。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())
        exc = CurationReadyBuildBlockedError(
            CurationReadyBuildBlockedCode.ARTICLE_MISSING
        )
        with (
            _patch_try_advance_from(exc),
            patch("app.queue.tasks.curation.CurationAuditRepository") as mock_audit,
            patch("app.queue.tasks.curation.CurationService"),
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_audit.return_value.append_ready_build_blocked = AsyncMock()
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        assert stage_attrs(capfire)["result"] == "skipped"

    @pytest.mark.asyncio
    async def test_ready_build_exception_sets_failed_via_backstop(
        self, capfire: CaptureLogfire
    ) -> None:
        """Ready 構築例外 (task は result 不設定) → backstop が failed を焼く。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake())
        exc = RuntimeError("ready build exploded")
        with (
            patch.object(
                ReadyForCuration,
                "try_advance_from",
                new=AsyncMock(side_effect=exc),
            ),
            patch(
                "app.queue.tasks.curation._append_ready_build_failed_audit",
                new=AsyncMock(),
            ),
            patch("app.queue.tasks.curation.CurationService"),
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
        ):
            mock_assess.kiq = AsyncMock()
            with pytest.raises(RuntimeError):
                await curate_content(trigger=_trigger(), ctx=mock_ctx)

        assert stage_attrs(capfire)["result"] == "failed"

    @pytest.mark.asyncio
    async def test_gate_skip_sets_rate_limited(self, capfire: CaptureLogfire) -> None:
        """gate.acquire=False 経路で task が result=rate_limited を焼く。"""
        from app.queue.tasks.curation import curate_content

        mock_ctx = _make_ctx(curator=_make_provider_fake(), gate_acquire=False)
        with (
            _patch_try_advance_from(_fixed_ready()),
            patch("app.queue.tasks.curation.CurationService"),
            patch("app.queue.tasks.curation.assess_content") as mock_assess,
            patch("app.queue.tasks.curation.record_rate_limit_gate_skipped"),
        ):
            mock_assess.kiq = AsyncMock()
            await curate_content(trigger=_trigger(), ctx=mock_ctx)

        assert stage_attrs(capfire)["result"] == "rate_limited"
