"""ESA/Webb (James Webb Space Telescope) News Fetcher (Phase 3 PR 3-b)。

NASA + ESA + CSA 共同運用。ESA 公式の RSS feed のため ESA 側 attribution
("ESA/Webb") を採用。

詳細は ``_common.py`` の docstring 参照。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.fetchers.esa._common import (
    BaseDjangoplicityAdapter,
)


class ESAWebbAdapter(BaseDjangoplicityAdapter):
    NAME: ClassVar[str] = "ESA/Webb"
    ENDPOINT_URL: ClassVar[str] = "https://esawebb.org/news/feed/"
