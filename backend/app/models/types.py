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
from app.shared.security.safe_url import SafeUrl


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


class SafeUrlType(TypeDecorator[SafeUrl]):
    """SafeUrl <-> VARCHAR(2048).

    ``CanonicalArticleUrl`` も同じ列に書き込めるように bind 側で受容する。
    canonical 値は SafeUrl の不変条件を満たすため、DB 側の物理表現は変わらず、
    Repository signature を ``CanonicalArticleUrl`` に上げても ORM 列の型は
    SafeUrl のままで透過処理できる (記事 identity の SSoT を型に寄せる目的)。
    """

    impl = String(2048)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, CanonicalArticleUrl):
            return value.root
        if isinstance(value, SafeUrl):
            return value.root
        if isinstance(value, str):
            return SafeUrl(value).root
        raise TypeError(
            f"Expected SafeUrl, CanonicalArticleUrl or str, got {type(value).__name__}"
        )

    def process_result_value(self, value: Any, dialect: Dialect) -> SafeUrl | None:
        if value is None:
            return None
        return SafeUrl(value)
