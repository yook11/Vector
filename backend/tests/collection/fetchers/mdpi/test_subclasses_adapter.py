"""MDPI 4 journal の registry 束縛整合性テスト (P2)。

P1 までは 4 thin subclass の ClassVar (``NAME`` / ``ISSN`` / ``JOURNAL_NAME``)
が per-journal 識別を持っていた。P2 でこれらは ``MDPICrossrefAdapter`` 汎用
machinery + ``strategy.py`` の ``ArticleSource`` factory に移った。

byte 不変の核は **``name → issn`` 束縛**: MDPI Materials の取得は必ず ISSN
``1996-1944`` で Crossref を引く、という per-journal の識別が P1 時点と完全
一致すること。継承 ClassVar が消えても束縛は壊れてはならないため、
``SOURCES`` の factory が組み立てる ``MDPICrossrefAdapter`` の注入 ISSN /
source_name を構造的に pin する (factory output の white-box 検査は、
本テストの目的=配線 drift 検出に対する正しい観測点)。
"""

from __future__ import annotations

import pytest

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.mdpi._common import MDPICrossrefAdapter
from app.collection.fetchers.strategy import SOURCES
from app.shared.value_objects.source_name import SourceName


@pytest.mark.parametrize(
    "name,issn",
    [
        ("MDPI Materials", "1996-1944"),
        ("MDPI Energies", "1996-1073"),
        ("MDPI Sensors", "1424-8220"),
        ("MDPI Nanomaterials", "2079-4991"),
    ],
)
def test_registry_binds_journal_name_to_issn(name: str, issn: str) -> None:
    source = SOURCES[SourceName(name)]
    # 補完方針 / 取得出自は MDPI 共通 (feed + DEFAULT、Pattern R)
    assert source.observed_origin is ObservedOrigin.feed
    assert source.completion_profile is DEFAULT_PROFILE

    adapter = source.make_adapter()
    assert isinstance(adapter, MDPICrossrefAdapter)
    # name → issn 束縛が P1 と一致 (factory 注入値の drift 検出)
    assert adapter._issn == issn
    assert adapter._source_name == name
