"""抽出素材の整形と公開日時変換の契約。"""

from datetime import UTC, datetime

from app.collection.article_completion.content import ScrapedContent
from app.collection.domain.article_limits import (
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
)
from app.collection.domain.value_objects import PublishedAt


class TestScrapedContentFromExtraction:
    """完成条件を判定せず、抽出された値を整形する。"""

    def test_html_tags_stripped_from_title(self) -> None:
        """タイトルのタグ・entity・前後空白を整形する。"""
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = ScrapedContent.from_extraction(
            raw_title="  <b>Bold &amp; Title</b>  ", stripped_body=body, raw_date=None
        )
        assert outcome.title == "Bold & Title"

    def test_title_over_limit_is_truncated(self) -> None:
        """長いタイトルは既存の上限で切り詰める。"""
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = ScrapedContent.from_extraction(
            raw_title="t" * (ARTICLE_TITLE_MAX_LENGTH + 10),
            stripped_body=body,
            raw_date=None,
        )
        assert outcome.title == "t" * ARTICLE_TITLE_MAX_LENGTH

    def test_parseable_date_populates_published_at(self) -> None:
        """解析可能な公開日時を既存の値オブジェクトへ変換する。"""
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = ScrapedContent.from_extraction(
            raw_title="Title", stripped_body=body, raw_date="2026-03-15T10:30:00"
        )
        assert outcome.published_at == PublishedAt(
            datetime(2026, 3, 15, 10, 30, tzinfo=UTC)
        )

    def test_unparseable_date_leaves_published_at_none(self) -> None:
        """解析不能な公開日時は欠如として保持する。"""
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = ScrapedContent.from_extraction(
            raw_title="Title", stripped_body=body, raw_date="not-a-date"
        )
        assert outcome.published_at is None

    def test_none_date_leaves_published_at_none(self) -> None:
        """公開日時の欠如をそのまま保持する。"""
        body = "x" * ARTICLE_BODY_MIN_LENGTH
        outcome = ScrapedContent.from_extraction(
            raw_title="Title", stripped_body=body, raw_date=None
        )
        assert outcome.published_at is None
