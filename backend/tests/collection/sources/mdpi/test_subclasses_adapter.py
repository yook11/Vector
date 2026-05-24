"""MDPI 4 journal の registry 束縛整合性テスト (P2-D)。

P1 までは 4 thin subclass の ClassVar (``NAME`` / ``ISSN`` / ``JOURNAL_NAME``)
が per-journal 識別を持っていた。P2-D でこれらは独立した ``MDPIXxxSource``
クラス (``mdpi.py``、``mdpi_items`` 共通処理を共有) になり、
``strategy.py`` の ``SOURCES`` レジストリが ``name → クラスオブジェクト`` を
束ねる。

byte 不変の核は **``name → issn`` 束縛**: MDPI Materials の取得は必ず ISSN
``1996-1944`` で Crossref を引く、という per-journal の識別が P1 時点と完全
一致すること。継承 ClassVar が消えても束縛は壊れてはならないため、
``SOURCES`` の値が想定の ``MDPIXxxSource`` クラスそのものであり、その
``_ISSN`` ClassVar が P1 時点と一致することを構造的に pin する (registry
配線 drift 検出に対する正しい観測点 = 無 instantiation で読める class 属性)。
"""

from __future__ import annotations

import pytest

from app.collection.article_collection.strategy import SOURCES
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import DEFAULT_POLICY
from app.collection.sources.article_source import ArticleSource
from app.collection.sources.definitions.mdpi import (
    MDPIEnergiesSource,
    MDPIMaterialsSource,
    MDPINanomaterialsSource,
    MDPISensorsSource,
)
from app.shared.value_objects.source_name import SourceName


@pytest.mark.parametrize(
    "name,source_cls,issn",
    [
        ("MDPI Materials", MDPIMaterialsSource, "1996-1944"),
        ("MDPI Energies", MDPIEnergiesSource, "1996-1073"),
        ("MDPI Sensors", MDPISensorsSource, "1424-8220"),
        ("MDPI Nanomaterials", MDPINanomaterialsSource, "2079-4991"),
    ],
)
def test_registry_binds_journal_name_to_issn(
    name: str, source_cls: ArticleSource, issn: str
) -> None:
    source = SOURCES[SourceName(name)]
    # registry 値は想定の Source クラスオブジェクトそのもの (無 instantiation)
    assert source is source_cls
    # 補完方針 / 取得出自は MDPI 共通 (feed + DEFAULT、Pattern R)
    assert source.observed_origin is ObservedOrigin.feed
    assert source.completion_policy is DEFAULT_POLICY
    # name → issn 束縛が P1 と一致 (ClassVar 直読み、drift 検出)
    assert source_cls._ISSN == issn
    assert source.name == SourceName(name)
