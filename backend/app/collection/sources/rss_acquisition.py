"""RSS取得の宣言と、その宣言を公開するソース契約。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from app.collection.sources.source_metadata import SourceMetadata


class RssBodyPolicy(StrEnum):
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class RssAcquisition:
    feeds: tuple[str, ...]
    parse_mode: Literal["text", "bytes"]
    body_policy: RssBodyPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.feeds, tuple) or not self.feeds:
            raise ValueError("feeds must be a non-empty tuple")
        if any(not isinstance(url, str) or not url.strip() for url in self.feeds):
            raise ValueError("feeds must contain non-empty URL strings")
        if self.parse_mode not in ("text", "bytes"):
            raise ValueError("unsupported RSS parse mode")
        if not isinstance(self.body_policy, RssBodyPolicy):
            raise ValueError("unsupported RSS body policy")


@runtime_checkable
class RssSource(SourceMetadata, Protocol):
    @property
    def acquisition(self) -> RssAcquisition: ...
