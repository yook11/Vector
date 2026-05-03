"""ESA/Hubble News Fetcher (Phase 3 PR 3-b)。

ESA/Hubble は NASA + ESA 共同運用、image credit は "ESA/Hubble" を一次表記
として採用 (Hubble Space Telescope Science Institute = STScI も別 credit を
持つが、ESA 公式の RSS feed であるため ESA 側を採用)。

詳細は ``_common.py`` の docstring 参照。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.ingestion.fetchers.esa._common import BaseDjangoplicityFetcher


class ESAHubbleFetcher(BaseDjangoplicityFetcher):
    NAME: ClassVar[str] = "ESA/Hubble"
    ENDPOINT_URL: ClassVar[str] = "https://esahubble.org/news/feed/"
    SITE_NAME: ClassVar[str] = "ESA/Hubble"
    AUTHOR: ClassVar[str] = "ESA/Hubble"
