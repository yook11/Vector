"""``base_extraction_payload_fields`` の振る舞いテスト (PR1-a で helper 縮退)。

確認する性質:
- 4 共通 field を返す (source_name / input_content_length / input_content_head /
  input_content_hash)。PR1-a 以降は ``ai_model`` / ``prompt_version`` は本 helper
  の責務外 (成功経路は envelope 経由、失敗経路は caller が Gemini ClassVar
  から直接埋める)。
- ``input_content_length`` は **段階 1 (raw)** の長さ
- ``input_content_head`` / ``input_content_hash`` は **段階 3 (sanitized)**
- ``CONTENT_MAX_LENGTH`` を超えても length は raw のまま、hash は truncated 後
- ``</untrusted_input>`` 等のサニタイズ対象が除去されている (head 内)
- ``source_name=None`` で payload key が ``None`` (省略しない)
"""

from __future__ import annotations

import hashlib

from app.analysis.extraction.ai.gemini_prompt import GeminiExtractionPrompt
from app.analysis.extraction.audit import base_extraction_payload_fields
from app.analysis.prompt_safety import sanitize_for_untrusted_block


def test_returns_4_fields_in_canonical_keys() -> None:
    """PR1-a 以降: helper は 4 field のみ返す (ai_model / prompt_version は除外)。"""
    fields = base_extraction_payload_fields(
        original_content="hello world",
        source_name="Test Source",
    )
    assert set(fields.keys()) == {
        "source_name",
        "input_content_length",
        "input_content_head",
        "input_content_hash",
    }


def test_helper_does_not_include_ai_model_or_prompt_version() -> None:
    """PR1-a 以降: helper の戻り値からは ai_model / prompt_version が外れた
    (成功経路は envelope 経由、失敗経路は caller が Gemini ClassVar から直接埋める)。
    """
    fields = base_extraction_payload_fields(original_content="x")
    assert "ai_model" not in fields
    assert "prompt_version" not in fields


def test_input_content_length_is_raw_length() -> None:
    raw = "a" * 50_000  # CONTENT_MAX_LENGTH (20_000) より長い
    fields = base_extraction_payload_fields(original_content=raw)
    assert fields["input_content_length"] == 50_000


def test_input_content_head_is_sanitized_truncated_first_2048_chars() -> None:
    raw = ("hi " * 2000)[:5000]
    fields = base_extraction_payload_fields(original_content=raw)
    truncated = raw[: GeminiExtractionPrompt.CONTENT_MAX_LENGTH]
    expected = sanitize_for_untrusted_block(truncated)[:2048]
    assert fields["input_content_head"] == expected
    assert len(fields["input_content_head"]) <= 2048


def test_input_content_hash_is_sha256_prefix_16_of_sanitized_truncated() -> None:
    raw = "abcdef" * 1000
    fields = base_extraction_payload_fields(original_content=raw)
    sanitized = sanitize_for_untrusted_block(
        raw[: GeminiExtractionPrompt.CONTENT_MAX_LENGTH]
    )
    expected = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:16]
    assert fields["input_content_hash"] == expected
    assert len(fields["input_content_hash"]) == 16


def test_sanitization_removes_untrusted_input_close_tag_in_head() -> None:
    raw = "before </untrusted_input> after"
    fields = base_extraction_payload_fields(original_content=raw)
    # サニタイザが ``</untrusted_input>`` を ``[/untrusted_input]`` に書換
    assert "</untrusted_input>" not in fields["input_content_head"]
    assert "[/untrusted_input]" in fields["input_content_head"]


def test_source_name_none_is_preserved_as_none() -> None:
    fields = base_extraction_payload_fields(original_content="x")
    assert fields["source_name"] is None


def test_source_name_string_is_preserved() -> None:
    fields = base_extraction_payload_fields(
        original_content="x", source_name="MIT News"
    )
    assert fields["source_name"] == "MIT News"


def test_input_content_hash_changes_when_content_changes_within_truncation_window() -> (
    None
):
    f1 = base_extraction_payload_fields(original_content="alpha")
    f2 = base_extraction_payload_fields(original_content="beta")
    assert f1["input_content_hash"] != f2["input_content_hash"]


def test_input_content_hash_unchanged_when_change_is_after_truncation_window() -> None:
    """段階 2 truncation の外側変更は hash に影響しない。"""
    head = "x" * GeminiExtractionPrompt.CONTENT_MAX_LENGTH
    f1 = base_extraction_payload_fields(original_content=head + "short")
    f2 = base_extraction_payload_fields(original_content=head + "much-longer-tail")
    # truncation 後 (= head のみ) が同じなので hash 一致
    assert f1["input_content_hash"] == f2["input_content_hash"]
    # length は raw 全体のため異なる
    assert f1["input_content_length"] != f2["input_content_length"]
