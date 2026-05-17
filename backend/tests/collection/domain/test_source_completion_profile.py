"""``SourceCompletionProfile`` の業務不変条件テスト。

per-source 補完方針は composition root の純データ。検証する不変条件:

1. 全域性: ``AnalyzableField`` 3 つ全てに policy が必要。部分写像は
   ``__post_init__`` で ``ValueError`` (3 frozenset 分割案の矛盾を型で不能化)。
2. 生成後不変: ``policies`` は ``MappingProxyType`` でコピー固定され、
   内容書換が構造的に不能 (frozen dataclass でも内包 dict は可変なため)。
3. 既定 profile の policy 値が spec §7 等価表どおり。
"""

from __future__ import annotations

import pytest

from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    HTML_TITLE_PROFILE,
    AnalyzableField,
    FieldCompletionPolicy,
    SourceCompletionProfile,
)


def test_partial_policy_map_is_rejected() -> None:
    """3 field を全て埋めない部分写像は ``ValueError``。"""
    with pytest.raises(ValueError, match="missing policy"):
        SourceCompletionProfile(
            {AnalyzableField.title: FieldCompletionPolicy.observed_preferred}
        )


def test_policies_is_immutable_after_construction() -> None:
    """``policies`` は MappingProxyType で書換不能 (生成後不変)。"""
    with pytest.raises(TypeError):
        DEFAULT_PROFILE.policies[AnalyzableField.body] = (  # type: ignore[index]
            FieldCompletionPolicy.observed_preferred
        )


def test_default_profile_matches_equivalence_table() -> None:
    """DEFAULT: title/published_at=observed_preferred, body=html_required。"""
    p = DEFAULT_PROFILE.policies
    assert p[AnalyzableField.title] is FieldCompletionPolicy.observed_preferred
    assert p[AnalyzableField.body] is FieldCompletionPolicy.html_required
    assert p[AnalyzableField.published_at] is FieldCompletionPolicy.observed_preferred


def test_html_title_profile_matches_equivalence_table() -> None:
    """HTML_TITLE (旧 prefer_html_title=True): title=html_preferred のみ差分。"""
    p = HTML_TITLE_PROFILE.policies
    assert p[AnalyzableField.title] is FieldCompletionPolicy.html_preferred
    assert p[AnalyzableField.body] is FieldCompletionPolicy.html_required
    assert p[AnalyzableField.published_at] is FieldCompletionPolicy.observed_preferred
