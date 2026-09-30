"""全アプリケーションモデル共通の DeclarativeBase。

Alembic / テストはすべて ``Base.metadata`` を target metadata に
取り、ここに登録されたテーブルを単一メタデータとして参照する。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

from app.collection.sources.source_name import SourceName
from app.models.types import SourceNameType, WebUrlType
from app.shared.web_url import WebUrl


class Base(DeclarativeBase):
    """VO の type_annotation_map を備えた共通 DeclarativeBase。"""

    type_annotation_map = {  # noqa: RUF012
        WebUrl: WebUrlType,
        SourceName: SourceNameType,
    }
