"""``compute_call_signature`` の振る舞いテスト。

ADR §prompt_version の規律 で約束される性質:

- determinism: 同入力 → 同出力 (毎回再計算しても一致)
- length: 戻り値は SHA-256 prefix 8 文字 (16 進数)
- 5 要素 sensitivity: いずれかが 1 byte 変わると hash が変わる
- gen_config dict 順序非依存: ``{"a": 1, "b": 2}`` と ``{"b": 2, "a": 1}`` で同 hash
- None 許容: ``response_schema=None`` / ``system_instruction=None`` で raise しない
"""

from __future__ import annotations

import re

import pytest

from app.analysis.prompt_versions import compute_call_signature

_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def _baseline() -> dict:
    """sensitivity 検証で各要素を 1 つずつ変えるための基準入力。"""
    return {
        "prompt_template": "title: {title}\ncontent: {content}",
        "model": "gemini-2.5-flash-lite",
        "gen_config": {"temperature": 0.2, "max_output_tokens": 1024},
        "response_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
        },
        "system_instruction": "You are a helpful assistant.",
    }


def test_determinism_same_input_same_hash() -> None:
    """同入力で 10 回呼んでも全て一致する。"""
    base = _baseline()
    hashes = {compute_call_signature(**base) for _ in range(10)}
    assert len(hashes) == 1


def test_returns_8_char_hex() -> None:
    """戻り値は 8 文字の 16 進数 (SHA-256 prefix)。"""
    h = compute_call_signature(**_baseline())
    assert _HEX8.fullmatch(h) is not None


@pytest.mark.parametrize(
    ("field", "mutated_value"),
    [
        ("prompt_template", "title: {title}\ncontent: {content}!"),
        ("model", "gemini-2.5-pro"),
        ("gen_config", {"temperature": 0.3, "max_output_tokens": 1024}),
        (
            "response_schema",
            {"type": "object", "properties": {"title": {"type": "integer"}}},
        ),
        ("system_instruction", "You are a STRICT assistant."),
    ],
)
def test_each_field_change_changes_hash(field: str, mutated_value: object) -> None:
    """5 要素のいずれかを変えると hash が変わる。"""
    base = _baseline()
    baseline_hash = compute_call_signature(**base)
    mutated = {**base, field: mutated_value}
    assert compute_call_signature(**mutated) != baseline_hash


def test_gen_config_dict_order_does_not_matter() -> None:
    """``json.dumps(sort_keys=True)`` が dict 順序差を吸収する。"""
    base = _baseline()
    h1 = compute_call_signature(
        **{**base, "gen_config": {"temperature": 0.2, "max_output_tokens": 1024}}
    )
    h2 = compute_call_signature(
        **{**base, "gen_config": {"max_output_tokens": 1024, "temperature": 0.2}}
    )
    assert h1 == h2


def test_response_schema_dict_order_does_not_matter() -> None:
    """response_schema の dict 順序差も同様に吸収。"""
    base = _baseline()
    h1 = compute_call_signature(
        **{
            **base,
            "response_schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
            },
        }
    )
    h2 = compute_call_signature(
        **{
            **base,
            "response_schema": {
                "properties": {"title": {"type": "string"}},
                "type": "object",
            },
        }
    )
    assert h1 == h2


def test_response_schema_none_does_not_raise() -> None:
    """``response_schema=None`` で例外を投げない。"""
    base = _baseline()
    h = compute_call_signature(**{**base, "response_schema": None})
    assert _HEX8.fullmatch(h) is not None


def test_system_instruction_none_does_not_raise() -> None:
    """``system_instruction=None`` で例外を投げない。"""
    base = _baseline()
    h = compute_call_signature(**{**base, "system_instruction": None})
    assert _HEX8.fullmatch(h) is not None


def test_response_schema_none_differs_from_empty_dict() -> None:
    """``None`` と ``{}`` は別物 (空 dict は ``"{}"`` をハッシュに混ぜる)。"""
    base = _baseline()
    h_none = compute_call_signature(**{**base, "response_schema": None})
    h_empty = compute_call_signature(**{**base, "response_schema": {}})
    assert h_none != h_empty
