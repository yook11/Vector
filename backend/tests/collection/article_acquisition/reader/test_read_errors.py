"""``UnreadableResponseReason`` / ``UnreadableResponseError`` の read 段 origin
契約テスト。

接続境界 [test_external_fetch_error_codes.py](../../test_external_fetch_error_codes.py)
の read 姉妹。reason.value がそのまま audit ``outcome_code`` に焼かれる自己記述
コードであること、origin error が PII-free な既定 message を合成すること (生上流を
載せない構造保証) を固定する。read 失敗は実質すべて terminal なので retryable 属性は
持たない (marker 側で ``NON_RETRYABLE`` 固定)。
"""

from __future__ import annotations

import pytest

from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.external_fetch_errors import ExternalFetchError

# reason の value (= outcome_code) 集合の spec-lock。class rename には不変で、
# 分類 drift / prefix ズレでのみ落ちる自己記述的 oracle。
_EXPECTED_REASON_CODES = frozenset(
    {
        "read_empty_body",
        "read_malformed_content",
        "read_unexpected_root_shape",
        "read_unexpected_field_shape",
    }
)


def test_reason_value_is_audit_outcome_code() -> None:
    """各 reason の value == ``read_<name.lower()>`` (自己記述コード規約)。"""
    for member in UnreadableResponseReason:
        assert member.value == f"read_{member.name.lower()}"


def test_reason_values_cover_spec_exactly() -> None:
    """reason 集合が spec と過不足なく一致する (totality)。"""
    assert {m.value for m in UnreadableResponseReason} == _EXPECTED_REASON_CODES


def test_reason_values_are_read_prefixed() -> None:
    """全 reason は ``read_`` prefix (接続 family の ``fetch_`` と別カテゴリ)。"""
    for member in UnreadableResponseReason:
        assert member.value.startswith("read_")


@pytest.mark.parametrize("reason", list(UnreadableResponseReason))
def test_code_property_is_reason_value(reason: UnreadableResponseReason) -> None:
    """``CODE`` は instance ごとに reason.value を公開する。

    marker base が ``origin.CODE`` を ``outcome_code`` に焼く配線 (fetch family の
    ClassVar ``CODE`` と同じ getattr 経路) が無改修で動くことの witness。
    """
    exc = UnreadableResponseError(reason=reason, response_format="json")
    assert exc.CODE == reason.value


def test_origin_preserves_safe_context() -> None:
    """reason / response_format / field / parser_position を instance に保持する。"""
    exc = UnreadableResponseError(
        reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
        response_format="json",
        field="items",
        parser_position="3:7",
    )
    assert exc.reason is UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE
    assert exc.response_format == "json"
    assert exc.field == "items"
    assert exc.parser_position == "3:7"


def test_default_message_is_pii_free_and_self_describing() -> None:
    """既定 message は安全値のみで合成し reason / format / field / position を含む。

    文字列を厳密一致で固定する: 将来 raw 上流値を message に補間する退行が入れば
    この一致が壊れて落ちる (PII-free の非空虚 oracle。そもそも constructor に raw を
    渡す引数が存在しないのが第一の構造保証)。
    """
    exc = UnreadableResponseError(
        reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
        response_format="json",
        field="items",
        parser_position="3:7",
    )
    assert str(exc) == "read_unexpected_field_shape: json field=items at=3:7"


def test_default_message_omits_absent_context() -> None:
    """field / parser_position が無ければ message から省く (reason+format は必須)。"""
    exc = UnreadableResponseError(
        reason=UnreadableResponseReason.EMPTY_BODY, response_format="xml"
    )
    assert str(exc) == "read_empty_body: xml"


def test_explicit_message_takes_precedence() -> None:
    """明示 message を渡せばそれが ``str`` になる (additive 非破壊・fetch と対称)。"""
    exc = UnreadableResponseError(
        "explicit boom",
        reason=UnreadableResponseReason.MALFORMED_CONTENT,
        response_format="feed",
    )
    assert str(exc) == "explicit boom"


def test_is_not_a_connection_error() -> None:
    """接続境界 ``ExternalFetchError`` family とは独立した別系統 (継承しない)。"""
    exc = UnreadableResponseError(
        reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="json"
    )
    assert not isinstance(exc, ExternalFetchError)
    assert not issubclass(UnreadableResponseError, ExternalFetchError)
