"""4 MDPI subclass の ClassVar 整合性テスト。"""

from __future__ import annotations

import pytest

from app.collection.fetchers.mdpi._common import BaseMDPICrossrefFetcher
from app.collection.fetchers.mdpi.energies import MDPIEnergiesFetcher
from app.collection.fetchers.mdpi.materials import MDPIMaterialsFetcher
from app.collection.fetchers.mdpi.nanomaterials import (
    MDPINanomaterialsFetcher,
)
from app.collection.fetchers.mdpi.sensors import MDPISensorsFetcher


@pytest.mark.parametrize(
    "klass,name,issn,journal",
    [
        (MDPIMaterialsFetcher, "MDPI Materials", "1996-1944", "Materials"),
        (MDPIEnergiesFetcher, "MDPI Energies", "1996-1073", "Energies"),
        (MDPISensorsFetcher, "MDPI Sensors", "1424-8220", "Sensors"),
        (
            MDPINanomaterialsFetcher,
            "MDPI Nanomaterials",
            "2079-4991",
            "Nanomaterials",
        ),
    ],
)
def test_classvar_consistency(
    klass: type[BaseMDPICrossrefFetcher],
    name: str,
    issn: str,
    journal: str,
) -> None:
    assert klass.NAME == name
    assert klass.ISSN == issn
    assert klass.JOURNAL_NAME == journal
    assert klass.LANGUAGE == "en"
    assert klass.PROVIDES == BaseMDPICrossrefFetcher.PROVIDES
    assert klass.ENDPOINT_URL == BaseMDPICrossrefFetcher.ENDPOINT_URL
