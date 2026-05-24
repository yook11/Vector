"""``complete_with_html`` (profile 駆動 promotion) の業務不変条件テスト。

旧 ``IncompleteArticle.complete_with_html`` (instance method) の後継。検証は
実装追跡ではなく **spec §7 等価表の回帰防止**:

- title ``html_preferred``: HTML title が正本になる (旧 anthropic/ornl)
- title ``observed_preferred``: 観測 title が常勝 (旧 default。観測常在のため)
- published_at ``observed_preferred``: 観測優先 / HTML fallback / 両欠は
  ``CompletionRejection`` (必須 Field 違反として畳む)
- **観測 body があっても ``html_required`` のとき完成 body は HTML 由来**
  (取れた事実を全部保存しても merge は不変 = forward-compat の核)
- ``AnalyzableArticle`` invariant 違反は ``CompletionRejection`` で wrap
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.collection.article_completion.completer import complete_with_html
from app.collection.article_completion.completion_failure import (
    CompletionRejection,
)
from app.collection.article_completion.scraper import ScrapedContent
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
)
from app.shared.value_objects.source_name import SourceName

_URL = CanonicalArticleUrl("https://example.com/article")
_OBS_PUB = PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC))
_HTML_PUB = PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC))


def _observed(
    *,
    title: str | None = "Observed Title",
    body: str | None = None,
    published: PublishedAt | None = _OBS_PUB,
) -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName("Example"),
        source_url=_URL,
        title=(
            ObservedField(value=title, origin=ObservedOrigin.feed)
            if title is not None
            else None
        ),
        body=(
            ObservedField(value=body, origin=ObservedOrigin.feed)
            if body is not None
            else None
        ),
        published_at=(
            ObservedField(value=published, origin=ObservedOrigin.feed)
            if published is not None
            else None
        ),
    )


def _html(
    *, title: str = "HTML Title", body: str = "h" * 200, published=_HTML_PUB
) -> ScrapedContent:
    return ScrapedContent(title=title, body=body, published_at=published)


def _promote(observed, profile, html, *, source_id=1):
    return complete_with_html(
        observed, profile, html, source_id=source_id, source_url=_URL
    )


def test_html_preferred_title_takes_html_authority() -> None:
    """``HTML_TITLE_POLICY`` は観測 title があっても HTML title を正本にする。"""
    result = _promote(
        _observed(title="Provisional"), HTML_TITLE_POLICY, _html(title="Real HTML")
    )
    assert isinstance(result, AnalyzableArticle)
    assert result.title == "Real HTML"


def test_observed_preferred_title_keeps_observed_authority() -> None:
    """``DEFAULT_POLICY`` は観測 title が常勝 (旧「常に self.title」と同値)。"""
    result = _promote(
        _observed(title="Feed Title"), DEFAULT_POLICY, _html(title="HTML Title")
    )
    assert isinstance(result, AnalyzableArticle)
    assert result.title == "Feed Title"


def test_published_at_observed_preferred_uses_observed() -> None:
    result = _promote(_observed(published=_OBS_PUB), DEFAULT_POLICY, _html())
    assert isinstance(result, AnalyzableArticle)
    assert result.published_at == _OBS_PUB


def test_published_at_falls_back_to_html_when_observed_absent() -> None:
    result = _promote(_observed(published=None), DEFAULT_POLICY, _html())
    assert isinstance(result, AnalyzableArticle)
    assert result.published_at == _HTML_PUB


def test_published_at_missing_both_fails_as_invariant_rejected() -> None:
    """published_at が観測 / HTML 両欠 → 必須 Field 違反として
    ``CompletionRejection`` に畳む (title/body/source_id と同種)。"""
    result = _promote(_observed(published=None), DEFAULT_POLICY, _html(published=None))
    assert isinstance(result, CompletionRejection)
    assert result.reason_code == "completion_invariant_rejected"
    assert result.detail is not None
    assert "published_at" in result.detail


def test_observed_body_is_ignored_when_body_html_required() -> None:
    """観測 body を保持しても ``html_required`` で完成 body は HTML 由来
    (事実の全保存が merge 挙動を変えない = spec §7 不変の核)。"""
    result = _promote(
        _observed(body="OBSERVED BODY " * 10),
        DEFAULT_POLICY,
        _html(body="HTML_AUTHORITATIVE_BODY " * 10),
    )
    assert isinstance(result, AnalyzableArticle)
    assert result.body.startswith("HTML_AUTHORITATIVE_BODY")
    assert "OBSERVED BODY" not in result.body


def test_analyzable_invariant_violation_wrapped_as_invariant_rejected() -> None:
    """``AnalyzableArticle`` の Field invariant 違反は名前付き失敗に畳む。"""
    result = _promote(_observed(), DEFAULT_POLICY, _html(), source_id=0)
    assert isinstance(result, CompletionRejection)
    assert result.reason_code == "completion_invariant_rejected"
    assert result.detail is not None
    assert "source_id" in result.detail
