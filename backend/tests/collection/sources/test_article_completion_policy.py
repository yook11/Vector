"""``ArticleCompletionPolicy`` の業務不変条件テスト。

per-source 補完方針は composition root の純データ。検証する不変条件:

1. 全域性: ``CompletableField`` 3 つ全てに policy が必要。部分写像は
   ``__post_init__`` で ``ValueError`` (3 frozenset 分割案の矛盾を型で不能化)。
2. 生成後不変: ``rules`` は ``MappingProxyType`` でコピー固定され、
   内容書換が構造的に不能 (frozen dataclass でも内包 dict は可変なため)。
3. 既定 profile の policy 値が spec §7 等価表どおり。
"""

from __future__ import annotations

import pytest

from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
    CompletableField,
    FieldCompletionRule,
)


def test_partial_policy_map_is_rejected() -> None:
    """3 field を全て埋めない部分写像は ``ValueError``。"""
    with pytest.raises(ValueError, match="missing rule"):
        ArticleCompletionPolicy(
            {CompletableField.title: FieldCompletionRule.observed_preferred}
        )


def test_rules_is_immutable_after_construction() -> None:
    """``rules`` は MappingProxyType で書換不能 (生成後不変)。"""
    with pytest.raises(TypeError):
        DEFAULT_POLICY.rules[CompletableField.body] = (  # type: ignore[index]
            FieldCompletionRule.observed_preferred
        )


def test_default_profile_matches_equivalence_table() -> None:
    """DEFAULT: title/published_at=observed_preferred, body=html_required。"""
    p = DEFAULT_POLICY.rules
    assert p[CompletableField.title] is FieldCompletionRule.observed_preferred
    assert p[CompletableField.body] is FieldCompletionRule.html_required
    assert p[CompletableField.published_at] is FieldCompletionRule.observed_preferred


def test_html_title_profile_matches_equivalence_table() -> None:
    """HTML_TITLE (旧 prefer_html_title=True): title=html_preferred のみ差分。"""
    p = HTML_TITLE_POLICY.rules
    assert p[CompletableField.title] is FieldCompletionRule.html_preferred
    assert p[CompletableField.body] is FieldCompletionRule.html_required
    assert p[CompletableField.published_at] is FieldCompletionRule.observed_preferred


def test_html_preferred_policy_requires_html_completion_for_any_field() -> None:
    """``html_preferred`` がどの field でも HTML 補完を要求する述語。

    title 以外 (body) が ``html_preferred`` でも True を返すことを固定し、
    旧 title 単独 gate ではなく per-field 導出であることを保証する。実 2
    profile は DEFAULT→False / HTML_TITLE→True (旧挙動と同値)。
    """
    body_html_preferred = ArticleCompletionPolicy(
        {
            CompletableField.title: FieldCompletionRule.observed_preferred,
            CompletableField.body: FieldCompletionRule.html_preferred,
            CompletableField.published_at: FieldCompletionRule.observed_preferred,
        }
    )
    assert body_html_preferred.requires_html_completion()
    assert not DEFAULT_POLICY.requires_html_completion()
    assert HTML_TITLE_POLICY.requires_html_completion()
