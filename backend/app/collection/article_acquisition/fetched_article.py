"""このアプリが収集対象として受け付けた記事。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.collection.article_acquisition.raw_article import RawArticle
from app.collection.domain.article_limits import ARTICLE_TITLE_MAX_LENGTH
from app.collection.domain.article_url import ArticleUrl
from app.shared.errors import ApplicationError


class FetchedArticleTitleMissingError(ApplicationError):
    """取得した記事に、受け付けに必要な title が無い。"""

    def __init__(self) -> None:
        super().__init__("取得した記事に title がありません")


@dataclass(frozen=True, slots=True)
class FetchedArticle:
    """記事の同一性に使う URL と、空でない title を持つ記事。"""

    url: ArticleUrl
    title: str
    body: str | None
    published_at: datetime | None

    @classmethod
    def from_raw(cls, raw: RawArticle) -> FetchedArticle:
        """取得材料を受け付け、URL が記事 URL にならないか title が空なら送出する。

        Raises:
            ArticleUrlInvalidError: URL を記事 URL にできない (空を含む)。
            FetchedArticleTitleMissingError: title が空。
        """
        url = ArticleUrl.from_raw(raw.url)
        title = raw.title.strip()[:ARTICLE_TITLE_MAX_LENGTH]
        if not title:
            raise FetchedArticleTitleMissingError()
        return cls(url=url, title=title, body=raw.body, published_at=raw.published_at)
