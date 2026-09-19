"""トップレベルの項目名の検査とdeny・allow判定の契約。"""

from unittest.mock import patch

import pytest

from app.log_policy.base import BASE_DENY
from app.log_policy.budget import TEXT_LIMIT
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.field_selection import LogFieldSelector

pytestmark = pytest.mark.unit


def test_allow_selection_has_no_implicit_metadata_allow() -> None:
    """基本項目も渡されたallowにない場合は未登録として扱う。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(frozenset(), BASE_DENY, diagnostics)
    assert not selector.select("event")
    assert diagnostics.as_fields() == {"_unregistered_count": 1}


def test_deny_precedes_allow() -> None:
    """denyとallowの両方に該当する項目は禁止項目として除外する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({"restricted_sample"}), frozenset({"restricted_sample"}), diagnostics
    )
    assert not selector.select("RestrictedSample")
    assert diagnostics.as_fields() == {"_denied_keys": ["RestrictedSample"]}


def test_similar_field_name_is_not_denied_by_partial_match() -> None:
    """禁止名を一部に含む項目を完全一致のdenyで巻き込まない。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({"completion_tokens"}), BASE_DENY, diagnostics
    )
    assert selector.select("completion_tokens")
    assert diagnostics.as_fields() == {}


@pytest.mark.parametrize("field_name", ["apiKey", "API_KEY", "api-key"])
def test_deny_normalizes_field_name_aliases(field_name: str) -> None:
    """項目名の表記揺れを正規化してdenyに照合する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(frozenset(), BASE_DENY, diagnostics)
    assert not selector.select(field_name)
    assert diagnostics.as_fields() == {"_denied_keys": [field_name]}


def test_allow_normalizes_registered_field_names() -> None:
    """allowは正規化して照合し、元の表記と異なっても採用する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(frozenset({"source_id"}), BASE_DENY, diagnostics)
    assert selector.select("sourceId")
    assert diagnostics.as_fields() == {}


def test_unregistered_field_name_is_not_recorded() -> None:
    """未登録項目は件数だけを診断に残し、入力名を記録しない。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(frozenset({"source_id"}), BASE_DENY, diagnostics)
    assert not selector.select("synthetic private name")
    assert diagnostics.as_fields() == {"_unregistered_count": 1}


def test_nonstring_field_name_is_unregistered() -> None:
    """非文字列の項目名は正規化へ渡す前に除外する。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(frozenset(), BASE_DENY, diagnostics)
    with patch("app.log_policy.field_selection.normalize_key") as normalize:
        assert not selector.select(7)
    assert diagnostics.as_fields() == {"_unregistered_count": 1}
    normalize.assert_not_called()


@pytest.mark.parametrize(
    "field_name",
    [
        "stack",
        "stack_info",
        "exception",
        "_record",
        "_from_structlog",
        "_log_policy_rules",
        "exc_info",
    ],
)
def test_internal_field_names_are_excluded_before_deny_and_allow(
    field_name: str,
) -> None:
    """内部項目はallowやdenyに含まれていても出力や診断へ残さない。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(
        frozenset({field_name}), frozenset({field_name}), diagnostics
    )
    assert not selector.select(field_name)
    assert diagnostics.as_fields() == {}


def test_field_name_at_length_limit_is_preserved() -> None:
    """長さの上限ちょうどの項目名はallowにあれば採用する。"""
    field_name = "a" * TEXT_LIMIT
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(frozenset({field_name}), BASE_DENY, diagnostics)
    assert selector.select(field_name)
    assert diagnostics.as_fields() == {}


def test_long_field_name_is_not_normalized() -> None:
    """長すぎる項目名は正規化へ渡さず上限診断だけを残す。"""
    diagnostics = LogProcessingDiagnostics()
    selector = LogFieldSelector(frozenset(), BASE_DENY, diagnostics)
    with patch("app.log_policy.field_selection.normalize_key") as normalize:
        assert not selector.select("a" * (TEXT_LIMIT + 1))
    assert diagnostics.as_fields() == {"_policy_limited": True}
    normalize.assert_not_called()
