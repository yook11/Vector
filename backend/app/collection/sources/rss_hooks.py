"""RSSソースで必要になる固有処理の呼び出し契約。"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.collection.article_acquisition.reader.rss_reader import RssEntry


@runtime_checkable
class RequiresBodyTransform(Protocol):
    def transform_body(self, body: str) -> str: ...


@runtime_checkable
class RequiresUrlTransform(Protocol):
    def transform_url(self, url: str) -> str: ...


@runtime_checkable
class RequiresPublishedAtResolution(Protocol):
    def resolve_published_at(self, entry: RssEntry) -> datetime | None: ...


@runtime_checkable
class RequiresScopeFilter(Protocol):
    def in_scope(self, entry: RssEntry) -> bool: ...


@runtime_checkable
class RequiresSelection(Protocol):
    def select(self, entries: list[RssEntry]) -> list[RssEntry]: ...
