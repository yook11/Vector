"""Frontiers 取得経路 (``FrontiersAISource`` + ``frontiers_entries`` 共通処理)。

P2-D で Frontiers 4 journal は ``frontiers_entries`` 共通処理を共有する独立
Source クラス (``frontiers/sources.py``) になった。固定する固有不変条件:

- ``description`` が 50 chars 未満の editorial/correction entry は
  ``frontiers_entries`` 内で business critical drop される
  (旧 ``BaseFrontiersFetcher`` の body<50 drop 移植証明)。identity の固定は
  test_source_adapter_profiles に集約。
"""

from __future__ import annotations

from app.collection.fetchers.frontiers.sources import FrontiersAISource
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from tests.collection.fetchers._fixture_tools import fixture_tools

_FIXTURE = "frontiers_ai_rss.xml"


async def test_short_description_entry_is_dropped() -> None:
    """fixture は通常記事 (desc≈613) + Editorial (desc=16) の 2 件。
    body<50 の Editorial は drop され通常記事のみ yield される。"""
    tools = fixture_tools(rss_fixture=_FIXTURE)

    items: list[FetchedArticle] = [
        item async for item in FrontiersAISource.collect(tools)
    ]

    assert len(items) == 1
    assert all(item.body is not None and len(item.body) >= 50 for item in items)
    assert not any(item.title.startswith("Editorial:") for item in items)
