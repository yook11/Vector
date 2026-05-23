"""``ArticleCompletionPolicy`` の業務不変条件テスト。

per-source 補完方針は composition root の純データ。検証する不変条件:

1. 全域性: ``CompletableField`` 3 つ全てに policy が必要。部分写像は
   ``__post_init__`` で ``ValueError`` (3 frozenset 分割案の矛盾を型で不能化)。
2. 生成後不変: ``rules`` は ``MappingProxyType`` でコピー固定され、
   内容書換が構造的に不能 (frozen dataclass でも内包 dict は可変なため)。
3. 既定 profile の policy 値が spec §7 等価表どおり。
4. ``resolve`` 写像: observed/html を per-field rule で merge する正本
   (spec §7 等価表の所有テスト。construct はしない=None でも値で返す)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
    CompletableField,
    FieldCompletionRule,
    ResolvedFields,
)

_OBS_PUB = PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC))
_HTML_PUB = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))


def _resolve_fields(
    policy: ArticleCompletionPolicy,
    *,
    observed_title: str | None = None,
    html_title: str | None = None,
    observed_body: str | None = None,
    html_body: str | None = None,
    observed_published_at: PublishedAt | None = None,
    html_published_at: PublishedAt | None = None,
) -> ResolvedFields:
    """``resolve`` への forwarding helper (各テストは関係 field のみ指定する)。

    ロジックは複製せず production の ``resolve`` を呼ぶだけ。
    """
    return policy.resolve(
        observed_title=observed_title,
        html_title=html_title,
        observed_body=observed_body,
        html_body=html_body,
        observed_published_at=observed_published_at,
        html_published_at=html_published_at,
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


# ---------------------------------------------------------------------------
# resolve 写像 (per-field source priority。spec §7 等価表の所有テスト)
# ---------------------------------------------------------------------------


def test_resolve_title_authority_diverges_by_policy() -> None:
    """同一入力でも title の正本は policy で分岐する (写像である証拠)。

    DEFAULT(observed_preferred)→観測勝ち / HTML_TITLE(html_preferred)→HTML勝ち。
    """
    kwargs = {"observed_title": "OBS", "html_title": "HTML"}
    assert _resolve_fields(DEFAULT_POLICY, **kwargs).title == "OBS"
    assert _resolve_fields(HTML_TITLE_POLICY, **kwargs).title == "HTML"


def test_resolve_body_html_required_takes_html_even_with_observed() -> None:
    """body=html_required は観測 body があっても HTML を正本にする。"""
    resolved = _resolve_fields(
        DEFAULT_POLICY, observed_body="OBS_BODY", html_body="HTML_BODY"
    )
    assert resolved.body == "HTML_BODY"


def test_resolve_body_html_required_never_falls_back_to_observed() -> None:
    """body=html_required は HTML 欠落時に観測へ fallback しない (写像 totality)。"""
    resolved = _resolve_fields(DEFAULT_POLICY, observed_body="OBS_BODY", html_body=None)
    assert resolved.body is None


def test_resolve_published_at_observed_preferred_keeps_observed() -> None:
    """published_at=observed_preferred は観測値を正本にする。"""
    resolved = _resolve_fields(
        DEFAULT_POLICY,
        observed_published_at=_OBS_PUB,
        html_published_at=_HTML_PUB,
    )
    assert resolved.published_at == _OBS_PUB


def test_resolve_published_at_falls_back_to_html_when_observed_absent() -> None:
    """published_at=observed_preferred は観測欠で HTML に fallback する。"""
    resolved = _resolve_fields(
        DEFAULT_POLICY, observed_published_at=None, html_published_at=_HTML_PUB
    )
    assert resolved.published_at == _HTML_PUB


def test_resolve_published_at_both_absent_returns_none_not_failure() -> None:
    """両源欠でも resolve は None を返すだけ (失敗判定は completer の責務)。"""
    resolved = _resolve_fields(
        DEFAULT_POLICY, observed_published_at=None, html_published_at=None
    )
    assert resolved.published_at is None
