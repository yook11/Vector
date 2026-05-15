"""4 MDPI Adapter subclass の ClassVar 整合性テスト。"""

from __future__ import annotations

import pytest

from app.collection.fetchers.mdpi._common import BaseMDPICrossrefAdapter
from app.collection.fetchers.mdpi.energies import MDPIEnergiesAdapter
from app.collection.fetchers.mdpi.materials import MDPIMaterialsAdapter
from app.collection.fetchers.mdpi.nanomaterials import MDPINanomaterialsAdapter
from app.collection.fetchers.mdpi.sensors import MDPISensorsAdapter


@pytest.mark.parametrize(
    "klass,name,issn,journal",
    [
        (MDPIMaterialsAdapter, "MDPI Materials", "1996-1944", "Materials"),
        (MDPIEnergiesAdapter, "MDPI Energies", "1996-1073", "Energies"),
        (MDPISensorsAdapter, "MDPI Sensors", "1424-8220", "Sensors"),
        (
            MDPINanomaterialsAdapter,
            "MDPI Nanomaterials",
            "2079-4991",
            "Nanomaterials",
        ),
    ],
)
def test_adapter_classvar_consistency(
    klass: type[BaseMDPICrossrefAdapter],
    name: str,
    issn: str,
    journal: str,
) -> None:
    assert klass.NAME == name
    assert klass.ISSN == issn
    assert klass.JOURNAL_NAME == journal
    assert klass.LANGUAGE == "en"
    assert klass.ENDPOINT_URL == BaseMDPICrossrefAdapter.ENDPOINT_URL
