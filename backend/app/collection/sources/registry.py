"""source registry の安全な lookup helper。"""

from __future__ import annotations

from app.collection.article_acquisition.strategy import SOURCES
from app.collection.sources.article_completion_policy import ArticleCompletionPolicy
from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.source_name import SourceName


def completion_policy_for(source_name: SourceName) -> ArticleCompletionPolicy:
    """source registry から completion policy を取得する。"""
    try:
        return SOURCES[source_name].completion_policy
    except KeyError as exc:
        raise SourceNotRegisteredError() from exc
