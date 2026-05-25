"""brokers.py の composition root に関するテスト。"""

from unittest.mock import patch

import pytest
from pydantic import SecretStr
from taskiq import TaskiqState

from app.brokers import CADENCE_CRON
from app.collection.sources.fetch_cadence import FetchCadence


class TestCadenceCronMapping:
    """``CADENCE_CRON`` が全 tier を 5-field cron に写像する。"""

    def test_every_cadence_tier_has_a_cron(self) -> None:
        """tier → cron 写像が全 ``FetchCadence`` メンバを網羅する (全域性)。"""
        assert set(CADENCE_CRON) == set(FetchCadence)

    def test_each_cron_has_five_fields(self) -> None:
        """各 cron 式が 5 フィールド (taskiq cron 形式) であること。"""
        for cadence, cron in CADENCE_CRON.items():
            assert len(cron.split()) == 5, f"{cadence} cron must be 5-field: {cron!r}"


@pytest.mark.asyncio
async def test_wire_analysis_adapters_attaches_adapters_to_state() -> None:
    """broker_analysis の WORKER_STARTUP で adapter が state に attach される。

    Provider 選択を hardcode する設計 (Pure DI) を構造的に保証する。
    """
    from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
    from app.analysis.curation.ai.gemini import GeminiCurator
    from app.brokers import _wire_analysis_adapters

    state = TaskiqState()
    with (
        patch("app.analysis.curation.ai.gemini.settings") as mock_es,
        patch("app.analysis.assessment.ai.deepseek.settings") as mock_cs,
    ):
        mock_es.gemini_api_key = SecretStr("test-key")
        mock_cs.deepseek_api_key = SecretStr("test-key")
        await _wire_analysis_adapters(state)

    assert isinstance(state.curator, GeminiCurator)
    assert isinstance(state.assessor, DeepSeekAssessor)
