"""collection/extraction ドメイン層のユニットテスト (DB 不要)。

PublishedAt VO の TZ 不変条件、ArticleDraft の sanitize / 長さ境界 /
from_extracted、Article Entity の identity 不変条件 / from_draft、
Draft → Entity 変換の等価性を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.collection.extraction.domain.article import (
    _ARTICLE_BODY_MAX_LENGTH,
    _ARTICLE_BODY_MIN_LENGTH,
    _ARTICLE_TITLE_MAX_LENGTH,
    Article,
    ArticleDraft,
)
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.extraction.extractor import ExtractedContent


def _valid_body(length: int = _ARTICLE_BODY_MIN_LENGTH) -> str:
    """指定長の有効な本文文字列を作る (ASCII 'a' で埋める)。"""
    return "a" * length


def _draft(**overrides: object) -> ArticleDraft:
    """テスト用の有効な ArticleDraft を作る。"""
    base: dict[str, object] = {
        "title": "Sample Title",
        "body": _valid_body(),
        "published_at": None,
    }
    base.update(overrides)
    return ArticleDraft(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PublishedAt — parse / invariant
# ---------------------------------------------------------------------------


class TestPublishedAtParse:
    def test_parse_iso_datetime_assigns_utc(self) -> None:
        published = PublishedAt.parse("2026-04-01T12:34:56")
        assert published is not None
        assert published.value == datetime(2026, 4, 1, 12, 34, 56, tzinfo=UTC)

    def test_parse_date_only_assigns_utc_midnight(self) -> None:
        published = PublishedAt.parse("2026-04-01")
        assert published is not None
        assert published.value == datetime(2026, 4, 1, tzinfo=UTC)

    def test_parse_returns_none_for_none(self) -> None:
        assert PublishedAt.parse(None) is None

    def test_parse_returns_none_for_empty_string(self) -> None:
        assert PublishedAt.parse("") is None

    def test_parse_returns_none_for_unknown_format(self) -> None:
        assert PublishedAt.parse("April 1, 2026") is None


class TestPublishedAtInvariant:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            PublishedAt(datetime(2026, 4, 1, 12, 0, 0))


# ---------------------------------------------------------------------------
# ArticleDraft — sanitize / length / non-empty
# ---------------------------------------------------------------------------


class TestArticleDraftSanitize:
    def test_strips_html_tags_from_title(self) -> None:
        draft = _draft(title="<b>Hello</b>")
        assert draft.title == "Hello"

    def test_strips_html_tags_from_body(self) -> None:
        body_with_tags = "<p>" + _valid_body() + "</p>"
        draft = _draft(body=body_with_tags)
        assert draft.body == _valid_body()

    def test_removes_control_characters(self) -> None:
        draft = _draft(title="Hello\x00World")
        assert draft.title == "HelloWorld"

    def test_preserves_tab_and_newline(self) -> None:
        body = "line1\nline2\t" + "a" * _ARTICLE_BODY_MIN_LENGTH
        draft = _draft(body=body)
        assert "\n" in draft.body
        assert "\t" in draft.body

    def test_normalizes_unicode_nfkc(self) -> None:
        # 全角数字を半角化
        draft = _draft(title="Hello 123")
        assert draft.title == "Hello 123"


class TestArticleDraftLengthBounds:
    def test_accepts_title_at_max_length(self) -> None:
        draft = _draft(title="t" * _ARTICLE_TITLE_MAX_LENGTH)
        assert len(draft.title) == _ARTICLE_TITLE_MAX_LENGTH

    def test_rejects_title_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            _draft(title="t" * (_ARTICLE_TITLE_MAX_LENGTH + 1))

    def test_accepts_body_at_min_length(self) -> None:
        draft = _draft(body=_valid_body(_ARTICLE_BODY_MIN_LENGTH))
        assert len(draft.body) == _ARTICLE_BODY_MIN_LENGTH

    def test_rejects_body_below_min_length(self) -> None:
        with pytest.raises(ValidationError):
            _draft(body=_valid_body(_ARTICLE_BODY_MIN_LENGTH - 1))

    def test_rejects_body_over_max_length(self) -> None:
        with pytest.raises(ValidationError):
            _draft(body=_valid_body(_ARTICLE_BODY_MAX_LENGTH + 1))


class TestArticleDraftNotEmpty:
    def test_rejects_title_that_becomes_empty_after_sanitization(self) -> None:
        # HTML タグだけのタイトル → sanitize 後に空
        with pytest.raises(ValidationError):
            _draft(title="<br/><br/>")

    def test_rejects_whitespace_only_title(self) -> None:
        with pytest.raises(ValidationError):
            _draft(title="   ")


class TestArticleDraftFrozen:
    def test_frozen_title_assignment_rejected(self) -> None:
        draft = _draft()
        with pytest.raises(ValidationError):
            draft.title = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ArticleDraft.from_extracted
# ---------------------------------------------------------------------------


class TestArticleDraftFromExtracted:
    def test_copies_fields_verbatim(self) -> None:
        published = PublishedAt(datetime(2026, 4, 1, tzinfo=UTC))
        content = ExtractedContent(
            title="Title",
            body=_valid_body(),
            published_at=published,
        )
        draft = ArticleDraft.from_extracted(content)
        assert draft.title == "Title"
        assert draft.body == _valid_body()
        assert draft.published_at == published

    def test_handles_none_published_at(self) -> None:
        content = ExtractedContent(title="Title", body=_valid_body(), published_at=None)
        draft = ArticleDraft.from_extracted(content)
        assert draft.published_at is None

    def test_re_sanitizes_extracted_content(self) -> None:
        # extractor は strip_html_tags 済みだが、Draft も再サニタイズ責務を持つ
        content = ExtractedContent(
            title="Title\x00", body=_valid_body(), published_at=None
        )
        draft = ArticleDraft.from_extracted(content)
        assert "\x00" not in draft.title


# ---------------------------------------------------------------------------
# Article — identity / non-empty / __post_init__
# ---------------------------------------------------------------------------


def _article(**overrides: object) -> Article:
    base: dict[str, object] = {
        "id": 1,
        "discovered_article_id": 10,
        "title": "Title",
        "body": _valid_body(),
        "published_at": None,
        "created_at": datetime(2026, 4, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return Article(**base)  # type: ignore[arg-type]


class TestArticleIdentity:
    def test_rejects_zero_id(self) -> None:
        with pytest.raises(ValueError, match="id must be positive"):
            _article(id=0)

    def test_rejects_negative_id(self) -> None:
        with pytest.raises(ValueError, match="id must be positive"):
            _article(id=-1)

    def test_rejects_zero_discovered_article_id(self) -> None:
        with pytest.raises(ValueError, match="discovered_article_id must be positive"):
            _article(discovered_article_id=0)

    def test_rejects_negative_discovered_article_id(self) -> None:
        with pytest.raises(ValueError, match="discovered_article_id must be positive"):
            _article(discovered_article_id=-5)


class TestArticleNonEmpty:
    def test_rejects_empty_title(self) -> None:
        with pytest.raises(ValueError, match="title must be non-empty"):
            _article(title="")

    def test_rejects_empty_body(self) -> None:
        with pytest.raises(ValueError, match="body must be non-empty"):
            _article(body="")


class TestArticleFrozen:
    def test_frozen_dataclass_assignment_rejected(self) -> None:
        article = _article()
        with pytest.raises(AttributeError):
            article.id = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Article.from_draft
# ---------------------------------------------------------------------------


class TestArticleFromDraft:
    def test_synthesizes_identity_and_created_at(self) -> None:
        draft = _draft()
        created = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
        article = Article.from_draft(
            draft, id=42, discovered_article_id=100, created_at=created
        )
        assert article.id == 42
        assert article.discovered_article_id == 100
        assert article.created_at == created

    def test_preserves_draft_payload(self) -> None:
        published = PublishedAt(datetime(2026, 3, 1, tzinfo=UTC))
        draft = _draft(title="Hello", body=_valid_body(), published_at=published)
        article = Article.from_draft(
            draft,
            id=1,
            discovered_article_id=10,
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
        assert article.title == "Hello"
        assert article.body == _valid_body()
        assert article.published_at == published


# ---------------------------------------------------------------------------
# Round-trip: ExtractedContent → Draft → Entity
#
# Repository._to_domain (ORM → Entity) と from_draft (Draft → Entity) が
# 同じ Entity を組み立てられることを保証する基盤テスト。
# ORM 経路の検証は PR 2a で repository テストとして追加する。
# ---------------------------------------------------------------------------


class TestExtractedContentToEntityRoundTrip:
    def test_extracted_to_draft_to_entity_preserves_payload(self) -> None:
        published = PublishedAt(datetime(2026, 4, 1, tzinfo=UTC))
        content = ExtractedContent(
            title="Round Trip", body=_valid_body(), published_at=published
        )
        draft = ArticleDraft.from_extracted(content)
        article = Article.from_draft(
            draft,
            id=7,
            discovered_article_id=70,
            created_at=datetime(2026, 4, 2, tzinfo=UTC),
        )
        assert article.title == "Round Trip"
        assert article.body == _valid_body()
        assert article.published_at == published
