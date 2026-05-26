"""``build_curation_audit_input`` の振る舞いテスト。

curation Service / failure_handling が呼ぶ caller pre-compute helper。
``CurationAuditRepository.append_*`` の kwargs に展開される 3 field を SSoT として
返すことを確認する。

確認する性質:
- 3 field を返す (input_content_length / input_content_head / input_content_hash)。
  ``source_name`` は audit_repository 側で ``_resolve_source_name(article_id)`` から
  resolve するため本 helper の責務外。``ai_model`` / ``prompt_version`` も同様に
  本 helper の責務外 (成功経路は envelope 経由、失敗経路は curator property 経由)。
- ``input_content_length`` は **段階 1 (raw)** の長さ
- ``input_content_head`` / ``input_content_hash`` は **段階 3 (sanitized)**
- ``CONTENT_MAX_LENGTH`` を超えても length は raw のまま、hash は truncated 後
- ``</untrusted_input>`` 等のサニタイズ対象が除去されている (head 内)
- ``CurationAuditInput`` TypedDict として 3 key 固定 (caller の **kwargs 展開時の
  型推論が効く)
"""

from __future__ import annotations

import hashlib

from app.analysis.curation.ai.gemini_prompt import GeminiCurationPrompt
from app.analysis.curation.audit import (
    CurationAuditInput,
    build_curation_audit_input,
)
from app.analysis.prompt_safety import sanitize_for_untrusted_block


def test_returns_3_fields_in_canonical_keys() -> None:
    """helper は 3 field のみ返す (source_name / ai_model / prompt_version は除外)。"""
    fields = build_curation_audit_input(original_content="hello world")
    assert set(fields.keys()) == {
        "input_content_length",
        "input_content_head",
        "input_content_hash",
    }


def test_helper_excludes_source_name_ai_model_prompt_version() -> None:
    """helper の戻り値からは source_name / ai_model / prompt_version が外れる
    (source_name は audit 側 _resolve_source_name で、ai_model / prompt_version は
    envelope or curator property で埋まる)。
    """
    fields = build_curation_audit_input(original_content="x")
    assert "source_name" not in fields
    assert "ai_model" not in fields
    assert "prompt_version" not in fields


def test_input_content_length_is_raw_length() -> None:
    raw = "a" * 50_000  # CONTENT_MAX_LENGTH (20_000) より長い
    fields = build_curation_audit_input(original_content=raw)
    assert fields["input_content_length"] == 50_000


def test_input_content_head_is_sanitized_truncated_first_2048_chars() -> None:
    raw = ("hi " * 2000)[:5000]
    fields = build_curation_audit_input(original_content=raw)
    truncated = raw[: GeminiCurationPrompt.CONTENT_MAX_LENGTH]
    expected = sanitize_for_untrusted_block(truncated)[:2048]
    assert fields["input_content_head"] == expected
    assert len(fields["input_content_head"]) <= 2048


def test_input_content_hash_is_sha256_prefix_16_of_sanitized_truncated() -> None:
    raw = "abcdef" * 1000
    fields = build_curation_audit_input(original_content=raw)
    sanitized = sanitize_for_untrusted_block(
        raw[: GeminiCurationPrompt.CONTENT_MAX_LENGTH]
    )
    expected = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:16]
    assert fields["input_content_hash"] == expected
    assert len(fields["input_content_hash"]) == 16


def test_sanitization_removes_untrusted_input_close_tag_in_head() -> None:
    raw = "before </untrusted_input> after"
    fields = build_curation_audit_input(original_content=raw)
    # サニタイザが ``</untrusted_input>`` を ``[/untrusted_input]`` に書換
    assert "</untrusted_input>" not in fields["input_content_head"]
    assert "[/untrusted_input]" in fields["input_content_head"]


def test_input_content_hash_changes_when_content_changes_within_truncation_window() -> (
    None
):
    f1 = build_curation_audit_input(original_content="alpha")
    f2 = build_curation_audit_input(original_content="beta")
    assert f1["input_content_hash"] != f2["input_content_hash"]


def test_input_content_hash_unchanged_when_change_is_after_truncation_window() -> None:
    """段階 2 truncation の外側変更は hash に影響しない。"""
    head = "x" * GeminiCurationPrompt.CONTENT_MAX_LENGTH
    f1 = build_curation_audit_input(original_content=head + "short")
    f2 = build_curation_audit_input(original_content=head + "much-longer-tail")
    # truncation 後 (= head のみ) が同じなので hash 一致
    assert f1["input_content_hash"] == f2["input_content_hash"]
    # length は raw 全体のため異なる
    assert f1["input_content_length"] != f2["input_content_length"]


def test_typeddict_shape_is_3_keys_only() -> None:
    """``CurationAuditInput`` TypedDict は 3 key 固定 (caller kwargs 展開用 SSoT)。"""
    assert set(CurationAuditInput.__annotations__.keys()) == {
        "input_content_length",
        "input_content_head",
        "input_content_hash",
    }
