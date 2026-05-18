"""``app.observability.domain.payloads`` の単体テスト。

field schema を検証する:

- ``SourceFetchPayload``: failure path 専用 (fetcher_class + HTTP snapshot 系)。
  成功側 audit (件数 / breakdown 集計) は撤去済。
- ``ContentFetchPayload``: ``canonical_url`` field
- ``BasePipelineEventPayload``: ``extra="ignore"`` で未知 field を drop
  (rolling deploy 中に新 publisher → 旧 worker の読戻しを爆発させない)
"""

from __future__ import annotations

from app.observability.domain.payloads import (
    ContentFetchPayload,
    SourceFetchPayload,
)


class TestSourceFetchPayloadFailureSnapshot:
    """failure path で使う fetcher_class + HTTP snapshot 系 field。"""

    def test_defaults_are_none(self) -> None:
        payload = SourceFetchPayload()
        assert payload.fetcher_class is None
        assert payload.http_status is None
        assert payload.final_url is None
        assert payload.response_size is None
        assert payload.content_type is None
        assert payload.body_head is None

    def test_http_snapshot_fields_can_be_set(self) -> None:
        payload = SourceFetchPayload(
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


class TestSourceFetchPayloadConversionFields:
    """per-entry 変換棄却 (REJECTED) 用 ``conversion_*`` 構造化列。"""

    def test_conversion_fields_default_none(self) -> None:
        """全 optional default None — 既存 failure payload 組立に無回帰。"""
        payload = SourceFetchPayload()
        assert payload.conversion_analyzable_reason is None
        assert payload.conversion_observed_reason is None
        assert payload.conversion_raw_url is None
        assert payload.conversion_has_title is None
        assert payload.conversion_body_length is None
        assert payload.conversion_has_published_at is None

    def test_conversion_fields_serialize_to_json(self) -> None:
        """``conversion_*`` を与えると JSONB 焼付 (model_dump json) に乗る。"""
        payload = SourceFetchPayload(
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
        assert SourceFetchPayload.model_validate(dumped) == payload


class TestContentFetchPayloadAuditKeys:
    """``ContentFetchPayload`` の集計 key field 不変条件 (PR-E dual-fill)。"""

    def test_canonical_url_field_exists(self) -> None:
        payload = ContentFetchPayload(canonical_url="https://example.com/a")
        assert payload.canonical_url == "https://example.com/a"

    def test_canonical_url_defaults_none(self) -> None:
        payload = ContentFetchPayload()
        assert payload.canonical_url is None

    def test_unknown_field_dropped_silently(self) -> None:
        """未知 field は ``extra="ignore"`` で silent drop される。

        rolling deploy 中に新 publisher が焼いた未知 field 付き JSONB を旧
        worker が ``model_validate`` で読み戻しても ValidationError で死なない
        ことを保証する (kiq message envelope の ``extra="ignore"`` 既定との対称性)。
        """
        restored = ContentFetchPayload.model_validate(
            {
                "kind": "content_fetch",
                "canonical_url": "https://example.com/a",
                "future_field": "x",
            }
        )
        assert restored.canonical_url == "https://example.com/a"
        assert not hasattr(restored, "future_field")


class TestPayloadJsonSerialization:
    """JSONB 焼付経路: ``model_dump(mode='json')`` → Pydantic 再構築の往復。"""

    def test_source_fetch_roundtrip(self) -> None:
        original = SourceFetchPayload(
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
        restored = SourceFetchPayload.model_validate(dumped)
        assert restored == original

    def test_content_fetch_roundtrip(self) -> None:
        original = ContentFetchPayload(
            canonical_url="https://example.com/article/round",
            extractor_class="ArticleHtmlExtractor",
            body_length=12345,
        )
        dumped = original.model_dump(mode="json")
        restored = ContentFetchPayload.model_validate(dumped)
        assert restored == original
