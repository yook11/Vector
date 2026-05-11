"""brokers.py の composition root に関するテスト。"""

from unittest.mock import patch

import pytest
from pydantic import SecretStr
from taskiq import TaskiqState


@pytest.mark.asyncio
async def test_wire_analysis_adapters_attaches_adapters_to_state() -> None:
    """broker_analysis の WORKER_STARTUP で adapter が state に attach される。

    Provider 選択を hardcode する設計 (Pure DI) を構造的に保証する。
    """
    from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
    from app.analysis.extraction.extractor.gemini import GeminiExtractor
    from app.brokers import _wire_analysis_adapters

    state = TaskiqState()
    with (
        patch("app.analysis.extraction.extractor.gemini.settings") as mock_es,
        patch("app.analysis.assessment.ai.deepseek.settings") as mock_cs,
    ):
        mock_es.gemini_api_key = SecretStr("test-key")
        mock_cs.deepseek_api_key = SecretStr("test-key")
        await _wire_analysis_adapters(state)

    assert isinstance(state.extractor, GeminiExtractor)
    assert isinstance(state.assessor, DeepSeekAssessor)
