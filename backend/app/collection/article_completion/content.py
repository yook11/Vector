"""HTML応答と、完成条件の判定前に保持する抽出素材。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.collection.domain.article_limits import ARTICLE_TITLE_MAX_LENGTH
from app.collection.domain.value_objects import PublishedAt

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(text: str | None) -> str | None:
    """scrape した title 文字列から HTML タグを除去し entity を decode する。"""
    if text is None:
        return None
    cleaned = _HTML_TAG_RE.sub("", text)
    return html.unescape(cleaned).strip()


@dataclass(frozen=True, slots=True)
class RawResponse:
    """取得済みの応答をHTTPクライアントに依存せず保持する。"""

    url: str
    content_type: str | None
    charset_from_header: str | None
    content: bytes
    decoded_text: str


@dataclass(frozen=True)
class ScrapedContent:
    """取得済み情報との統合に使う、項目が不足していても保持できる素材。"""

    title: str | None
    body: str
    published_at: PublishedAt | None

    @classmethod
    def from_extraction(
        cls,
        *,
        raw_title: str | None,
        stripped_body: str,
        raw_date: str | None,
    ) -> ScrapedContent:
        """抽出値を整形し、完成記事の品質判定は統合後に委ねる。"""
        cleaned_title = _strip_html_tags(raw_title)
        title = cleaned_title[:ARTICLE_TITLE_MAX_LENGTH] if cleaned_title else None
        return cls(
            title=title,
            body=stripped_body.strip(),
            published_at=PublishedAt.parse(raw_date),
        )
