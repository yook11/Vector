"""``app.audit.domain.payloads`` の単体テスト。

field schema と payload field の所有権を検証する。
"""

from __future__ import annotations

from app.audit.domain.payloads import (
    AssessmentPayload,
    BackfillPayload,
    BasePipelineEventPayload,
    BriefingPayload,
    CurationPayload,
    DispatchPayload,
    EmbeddingPayload,
    TrendDiscoveryPayload,
)


class TestCurationPayloadReadyBuildFields:
    """Ready build blocked / failed 用の curation payload field。"""

    def test_ready_build_fields_can_be_set(self) -> None:
        payload = CurationPayload(
            target_article_id=123,
            input_content_length=200_001,
            max_content_length=200_000,
        )
        dumped = payload.model_dump(mode="json")

        assert dumped["target_article_id"] == 123
        assert dumped["input_content_length"] == 200_001
        assert dumped["max_content_length"] == 200_000


class TestDispatchPayloadAuditKeys:
    """``DispatchPayload`` の key field 不変条件。"""

    def test_dispatch_audit_fields_roundtrip(self) -> None:
        payload = DispatchPayload(
            source_name="TechCrunch",
            cadence="high",
            raw_source_name="TechCrunch",
        )
        dumped = payload.model_dump(mode="json")
        restored = DispatchPayload.model_validate(dumped)
        assert restored == payload

    def test_dispatch_payload_does_not_own_count_fields(self) -> None:
        # per-source / per-run の件数は metric (vector.dispatch.*) の責務。
        for removed in (
            "dispatched_count",
            "selected_count",
            "rejected_count",
            "failed_count",
        ):
            assert removed not in DispatchPayload.model_fields

    def test_dispatch_legacy_count_payload_is_readable(self) -> None:
        """撤去前 count field 入りの旧 dispatch payload を読み戻せる (保証6)。"""
        restored = DispatchPayload.model_validate(
            {
                "kind": "dispatch",
                "cadence": "all",
                "selected_count": 5,
                "dispatched_count": 4,
                "rejected_count": 1,
                "failed_count": 0,
            }
        )
        assert restored.cadence == "all"
        for removed in (
            "dispatched_count",
            "selected_count",
            "rejected_count",
            "failed_count",
        ):
            assert not hasattr(restored, removed)


class TestBackfillPayloadBackwardCompat:
    """撤去した throughput field を含む旧 JSONB の読み戻し互換 (保証6)。"""

    def test_backfill_legacy_count_payload_is_readable(self) -> None:
        """撤去前 count field 入りの旧 payload を ``model_validate`` で読める。

        rolling deploy 中に旧 worker が焼いた ``selected_count`` 等を含む JSONB を
        新 schema が読み戻しても ``extra="ignore"`` で死なず、撤去 field を
        instance に保持しないことを保証する。
        """
        restored = BackfillPayload.model_validate(
            {
                "kind": "backfill",
                "backfill_stage": "embed",
                "run_id": "run-legacy",
                "selected_count": 50,
                "granted_count": 10,
                "enqueued_count": 9,
                "failed_count": 1,
                "limit": 50,
                "daily_max": 1500,
            }
        )
        assert restored.backfill_stage == "embed"
        assert restored.run_id == "run-legacy"
        assert restored.daily_max == 1500
        for removed in (
            "selected_count",
            "granted_count",
            "enqueued_count",
            "failed_count",
            "limit",
        ):
            assert not hasattr(restored, removed)


class TestTrendDiscoveryPayloadAuditKeys:
    """``TrendDiscoveryPayload`` の run-level field contract。"""

    def test_trend_discovery_fields_roundtrip(self) -> None:
        payload = TrendDiscoveryPayload(
            window_start="2026-04-26",
            window_end="2026-05-03",
            source_analysis_count=42,
            completed_category_count=3,
            error_message="aggregation failed",
            error_chain=["builtins.RuntimeError"],
        )
        dumped = payload.model_dump(mode="json")
        restored = TrendDiscoveryPayload.model_validate(dumped)
        assert restored == payload


class TestBriefingPayloadAuditKeys:
    """``BriefingPayload`` の dispatch / generation field contract。"""

    def test_briefing_dispatch_count_fields_roundtrip(self) -> None:
        payload = BriefingPayload(
            week_start="2026-04-20",
            selected_category_count=3,
            enqueued_category_count=2,
            failed_category_count=1,
        )
        dumped = payload.model_dump(mode="json")
        restored = BriefingPayload.model_validate(dumped)
        assert restored == payload


class TestPayloadFieldOwnership:
    """top-level column にしない stage-local field の所有権を固定する。"""

    def test_failure_attributes_are_stage_payload_fields(self) -> None:
        stage_payloads = (
            CurationPayload,
            AssessmentPayload,
            EmbeddingPayload,
            BriefingPayload,
        )
        for payload_cls in stage_payloads:
            assert "failure_kind" in payload_cls.model_fields
            assert "failure_action" in payload_cls.model_fields

        assert "failure_kind" not in BasePipelineEventPayload.model_fields
        assert "failure_action" not in BasePipelineEventPayload.model_fields
        assert "failure_kind" not in DispatchPayload.model_fields
        assert "failure_action" not in DispatchPayload.model_fields
        assert "failure_kind" not in TrendDiscoveryPayload.model_fields
        assert "failure_action" not in TrendDiscoveryPayload.model_fields

    def test_failure_reason_owned_by_cause_axis_payloads(self) -> None:
        # 原因詳細 (provider reason 値) を焼くのは AI 分析 3 stage
        # (curation / assessment / embedding) のみ。それ以外の stage は reason 軸を
        # 持たないため failure_reason field を持たない。
        cause_axis_payloads = (
            CurationPayload,
            AssessmentPayload,
            EmbeddingPayload,
        )
        for payload_cls in cause_axis_payloads:
            assert "failure_reason" in payload_cls.model_fields
            # provider 由来でない失敗のため default は None。
            assert payload_cls().failure_reason is None

        payloads_without_failure_reason = (
            BasePipelineEventPayload,
            DispatchPayload,
            BriefingPayload,
            TrendDiscoveryPayload,
        )
        for payload_cls in payloads_without_failure_reason:
            assert "failure_reason" not in payload_cls.model_fields

    def test_injection_markers_present_owned_by_input_baking_payloads(self) -> None:
        """``injection_markers_present`` は外部入力を焼く curation だけが持ち、
        他 payload・base には載せない (普遍属性ではない)。
        """
        assert "injection_markers_present" in CurationPayload.model_fields

        payloads_without_flag = (
            BasePipelineEventPayload,
            DispatchPayload,
            AssessmentPayload,
            EmbeddingPayload,
            BriefingPayload,
            TrendDiscoveryPayload,
        )
        for payload_cls in payloads_without_flag:
            assert "injection_markers_present" not in payload_cls.model_fields
