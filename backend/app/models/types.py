"""ドメイン値オブジェクト向けの SQLAlchemy TypeDecorator 群。

各 TypeDecorator は VO（Python 側）とプレーン文字列（DB 側）を相互変換する。
生の str も受け付けるが、VO コンストラクタを通して検証される（バイパス不可）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.sources.source_name import SourceName
from app.shared.web_url import WebUrl


class SourceNameType(TypeDecorator[SourceName]):
    """SourceName <-> VARCHAR(50)."""

    impl = String(50)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, SourceName):
            return value.root
        if isinstance(value, str):
            return SourceName(value).root
        raise TypeError(f"Expected SourceName or str, got {type(value).__name__}")

    def process_result_value(self, value: Any, dialect: Dialect) -> SourceName | None:
        if value is None:
            return None
        return SourceName(value)


class WebUrlType(TypeDecorator[WebUrl]):
    """WebUrl <-> VARCHAR(2048).

    ``CanonicalArticleUrl`` も同じ列に書き込めるように bind 側で受容する。
    canonical 値は WebUrl の不変条件を満たすため、DB 側の物理表現は変わらず、
    Repository signature を ``CanonicalArticleUrl`` に上げても ORM 列の型は
    WebUrl のままで透過処理できる (記事 identity の SSoT を型に寄せる目的)。
    """

    impl = String(2048)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, CanonicalArticleUrl):
            return value.root
        if isinstance(value, WebUrl):
            return value.root
        if isinstance(value, str):
            return WebUrl(value).root
        raise TypeError(
            f"Expected WebUrl, CanonicalArticleUrl or str, got {type(value).__name__}"
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> WebUrl | None:
        if value is None:
            return None
        return WebUrl(value)
