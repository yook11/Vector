"""``parse_extraction`` の振る舞いテスト。

確認する性質:
- ``response.relevance == "signal"`` → ``Signal`` を返す
- ``response.relevance == "noise"`` → ``Noise`` を返す
- ``title_ja`` / ``summary_ja`` が round-trip で保持される
- ``GeminiExtractionResponse`` で sanitize 済の値はそのまま domain 型に渡る
"""

from __future__ import annotations

from app.analysis.extraction.ai.parse import parse_extraction
from app.analysis.extraction.ai.schema import GeminiExtractionResponse
from app.analysis.extraction.domain import Noise, Signal


def _gemini_response(relevance: str = "signal") -> GeminiExtractionResponse:
    return GeminiExtractionResponse(
        relevance=relevance,  # type: ignore[arg-type]
        title_ja="タイトル",
        summary_ja="要約",
    )


def test_signal_relevance_routes_to_signal() -> None:
    response = _gemini_response("signal")
    result = parse_extraction(response)
    assert isinstance(result, Signal)
    assert result.title_ja == "タイトル"
    assert result.summary_ja == "要約"


def test_noise_relevance_routes_to_noise() -> None:
    response = _gemini_response("noise")
    result = parse_extraction(response)
    assert isinstance(result, Noise)
    assert result.title_ja == "タイトル"
    assert result.summary_ja == "要約"
