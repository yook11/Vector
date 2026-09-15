"""source registry の安全な lookup helper。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.collection.sources.article_completion_policy import ArticleCompletionPolicy
from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.source_name import SourceName

if TYPE_CHECKING:
    from app.collection.sources.article_source import AcquirableSource


def acquisition_source_for(source_name: SourceName) -> AcquirableSource:
    """ソース名に対応する登録済みの取得定義を返す。"""
    # 定期投入の起動時に全ソース定義を読み込まない。
    from app.collection.article_acquisition.strategy import SOURCES

    try:
        return SOURCES[source_name]
    except KeyError as exc:
        raise SourceNotRegisteredError() from exc


def completion_policy_for(source_name: SourceName) -> ArticleCompletionPolicy:
    """source registry から completion policy を取得する。"""
    return acquisition_source_for(source_name).completion_policy
