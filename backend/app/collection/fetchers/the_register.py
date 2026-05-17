"""The Register 用 Source — Pattern H (Atom feed)。

The Register の Atom フィードは ``<summary>`` に短いリード文しか出さず、
本文は HTML を別途取得して trafilatura で抽出する必要がある。

per-source 設計 (実 Atom 観察ベース):

- feed 形式は **Atom (RFC4287)**、``xml:lang="en"``
- ``<link rel="alternate" href>`` は **redirector URL**
  (``https://go.theregister.com/feed/<host>/<path>``)、
  ``_normalize_register_link`` で実 URL に展開してから渡す
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.shared.value_objects.source_name import SourceName

_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


def _normalize_register_link(raw: str) -> str:
    """``go.theregister.com/feed/<host>/<path>`` → ``https://<host>/<path>`` に直す。

    The Register の Atom フィードは ``<link href>`` がリダイレクタ経由
    (``https://go.theregister.com/feed/www.theregister.com/2026/...``) のため、
    プレフィックスを切り捨てて実 URL を再構築する。
    """
    if raw.startswith(_REDIRECTOR_PREFIX):
        return "https://" + raw[len(_REDIRECTOR_PREFIX) :]
    return raw


class TheRegisterSource:
    """The Register 用 ``XxxSource`` (Pattern H、Atom feed)。

    ``<link href>`` は redirector 経由 (``go.theregister.com/feed/...``) のため
    ``_normalize_register_link`` で実 URL に展開してから渡す (builder では
    復元できない per-source 変換)。title / URL の構造ゲートは
    ``passport_builder`` に委譲する。
    """

    name: ClassVar[SourceName] = SourceName("The Register")
    endpoint_url: ClassVar[str] = "https://www.theregister.com/headlines.atom"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )
        for entry in entries:
            if not entry.link:
                continue
            yield FetchedArticle(
                title=entry.title,
                url=_normalize_register_link(entry.link),
                body=None,
                published_at=entry.published,
            )
