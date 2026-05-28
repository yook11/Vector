"""``app.audit.domain.payloads`` の単体テスト。

field schema と payload field の所有権を検証する。
"""

from __future__ import annotations

from app.audit.domain.payloads import (
    AcquisitionPayload,
    AssessmentPayload,
    BasePipelineEventPayload,
    BriefingPayload,
    CompletionPayload,
    CurationPayload,
    DispatchPayload,
    EmbeddingPayload,
    TrendDiscoveryPayload,
)


class TestAcquisitionPayloadFailureSnapshot:
    """acquisition failure snapshot field。"""

    def test_defaults_are_none(self) -> None:
        payload = AcquisitionPayload()
        assert payload.fetcher_class is None
        assert payload.http_status is None
        assert payload.final_url is None
        assert payload.response_size is None
        assert payload.content_type is None
        assert payload.body_head is None
        assert payload.failure_kind is None
        assert payload.failure_action is None

    def test_http_snapshot_fields_can_be_set(self) -> None:
        payload = AcquisitionPayload(
            fetcher_class="VentureBeatFetcher",
            http_status=403,
            final_url="https://venturebeat.com/feed/",
            response_size=1024,
            content_type="text/html",
            body_head="Forbidden",
        )
        assert payload.fetcher_class == "VentureBeatFetcher"
        assert payload.http_status == 403
        assert payload.final_url == "https://venturebeat.com/feed/"
        assert payload.response_size == 1024
        assert payload.content_type == "text/html"
        assert payload.body_head == "Forbidden"

    def test_failure_attribute_fields_can_be_set(self) -> None:
        payload = AcquisitionPayload(
            failure_kind="external_fetch",
            failure_action=None,
        )
        dumped = payload.model_dump(mode="json")
        assert dumped["failure_kind"] == "external_fetch"
        assert dumped["failure_action"] is None


class TestAcquisitionPayloadConversionFields:
    """per-entry 変換棄却 (REJECTED) 用 ``conversion_*`` 構造化列。"""

    def test_conversion_fields_default_none(self) -> None:
        """全 optional field の default は None。"""
        payload = AcquisitionPayload()
        assert payload.conversion_analyzable_reason is None
        assert payload.conversion_observed_reason is None
        assert payload.conversion_raw_url is None
        assert payload.conversion_has_title is None
        assert payload.conversion_body_length is None
        assert payload.conversion_has_published_at is None

    def test_conversion_fields_serialize_to_json(self) -> None:
        """``conversion_*`` を与えると JSONB 焼付 (model_dump json) に乗る。"""
        payload = AcquisitionPayload(
            conversion_analyzable_reason="body_too_short",
            conversion_observed_reason="missing_title",
            conversion_raw_url="https://example.com/a",
            conversion_has_title=True,
            conversion_body_length=42,
            conversion_has_published_at=False,
        )
        dumped = payload.model_dump(mode="json")
        assert dumped["conversion_analyzable_reason"] == "body_too_short"
        assert dumped["conversion_observed_reason"] == "missing_title"
        assert dumped["conversion_raw_url"] == "https://example.com/a"
        assert dumped["conversion_has_title"] is True
        assert dumped["conversion_body_length"] == 42
        assert dumped["conversion_has_published_at"] is False
        assert AcquisitionPayload.model_validate(dumped) == payload


class TestCompletionPayloadAuditKeys:
    """``CompletionPayload`` の key field 不変条件。"""

    def test_canonical_url_field_exists(self) -> None:
        payload = CompletionPayload(canonical_url="https://example.com/a")
        assert payload.canonical_url == "https://example.com/a"

    def test_canonical_url_defaults_none(self) -> None:
        payload = CompletionPayload()
        assert payload.canonical_url is None
        assert payload.attempt_count is None
        assert payload.failure_kind is None
        assert payload.failure_action is None

    def test_attempt_count_field_can_be_set(self) -> None:
        payload = CompletionPayload(attempt_count=3)
        dumped = payload.model_dump(mode="json")
        assert dumped["attempt_count"] == 3

    def test_failure_attribute_fields_can_be_set(self) -> None:
        payload = CompletionPayload(
            failure_kind="external_fetch",
            failure_action=None,
        )
        dumped = payload.model_dump(mode="json")
        assert dumped["failure_kind"] == "external_fetch"
        assert dumped["failure_action"] is None

    def test_unknown_field_dropped_silently(self) -> None:
        """未知 field は ``extra="ignore"`` で silent drop される。

        rolling deploy 中に新 publisher が焼いた未知 field 付き JSONB を旧
        worker が ``model_validate`` で読み戻しても ValidationError で死なない
        ことを保証する (kiq message envelope の ``extra="ignore"`` 既定との対称性)。
        """
        restored = CompletionPayload.model_validate(
            {
                "kind": "completion",
                "canonical_url": "https://example.com/a",
                "future_field": "x",
            }
        )
        assert restored.canonical_url == "https://example.com/a"
        assert not hasattr(restored, "future_field")


class TestPayloadJsonSerialization:
    """JSONB 焼付経路: ``model_dump(mode='json')`` → Pydantic 再構築の往復。"""

    def test_acquisition_roundtrip(self) -> None:
        original = AcquisitionPayload(
            fetcher_class="VBFetcher",
            http_status=403,
            final_url="https://venturebeat.com/feed/",
            response_size=1024,
            content_type="text/html",
            body_head="Forbidden",
            error_message="upstream returned 403",
            error_chain=["httpx.HTTPStatusError"],
        )
        dumped = original.model_dump(mode="json")
        restored = AcquisitionPayload.model_validate(dumped)
        assert restored == original

    def test_completion_roundtrip(self) -> None:
        original = CompletionPayload(
            canonical_url="https://example.com/article/round",
            attempt_count=2,
            scraper_class="ArticleScraper",
            body_length=12345,
        )
        dumped = original.model_dump(mode="json")
        restored = CompletionPayload.model_validate(dumped)
        assert restored == original


class TestDispatchPayloadAuditKeys:
    """``DispatchPayload`` の key field 不変条件。"""

    def test_dispatch_audit_fields_roundtrip(self) -> None:
        payload = DispatchPayload(
            source_name="TechCrunch",
            cadence="high",
            raw_source_name="TechCrunch",
            selected_count=1,
            dispatched_count=1,
            rejected_count=0,
            failed_count=0,
        )
        dumped = payload.model_dump(mode="json")
        restored = DispatchPayload.model_validate(dumped)
        assert restored == payload


class TestTrendDiscoveryPayloadAuditKeys:
    """``TrendDiscoveryPayload`` の run-level field contract。"""

    def test_trend_discovery_fields_roundtrip(self) -> None:
        payload = TrendDiscoveryPayload(
            window_start="2026-04-26",
            window_end="2026-05-03",
            trigger="cli",
            requested_update=True,
            source_analysis_count=42,
            completed_category_count=3,
            error_message="aggregation failed",
            error_chain=["builtins.RuntimeError"],
        )
        dumped = payload.model_dump(mode="json")
        restored = TrendDiscoveryPayload.model_validate(dumped)
        assert restored == payload


class TestPayloadFieldOwnership:
    """top-level column にしない stage-local field の所有権を固定する。"""

    def test_failure_attributes_are_stage_payload_fields(self) -> None:
        stage_payloads = (
            AcquisitionPayload,
            CompletionPayload,
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

    def test_only_completion_payload_owns_attempt_count(self) -> None:
        assert "attempt_count" in CompletionPayload.model_fields

        payloads_without_attempt_count = (
            BasePipelineEventPayload,
            DispatchPayload,
            AcquisitionPayload,
            CurationPayload,
            AssessmentPayload,
            EmbeddingPayload,
            BriefingPayload,
            TrendDiscoveryPayload,
        )
        for payload_cls in payloads_without_attempt_count:
            assert "attempt_count" not in payload_cls.model_fields
            assert "retry_attempt" not in payload_cls.model_fields
