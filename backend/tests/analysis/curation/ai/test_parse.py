"""``parse_curation`` の振る舞いテスト。

確認する性質:
- ``response.relevance == "signal"`` → ``Signal`` を返す
- ``response.relevance == "noise"`` → ``Noise`` を返す
- ``title_ja`` / ``summary_ja`` が round-trip で保持される
- ``GeminiCurationResponse`` で sanitize 済の値はそのまま domain 型に渡る
"""

from __future__ import annotations

from app.analysis.curation.ai.parse import parse_curation
from app.analysis.curation.ai.schema import GeminiCurationResponse
from app.analysis.curation.domain import Noise, Signal


def _gemini_response(relevance: str = "signal") -> GeminiCurationResponse:
    return GeminiCurationResponse(
        relevance=relevance,  # type: ignore[arg-type]
        title_ja="タイトル",
        summary_ja="要約",
    )


def test_signal_relevance_routes_to_signal() -> None:
    response = _gemini_response("signal")
    result = parse_curation(response)
    assert isinstance(result, Signal)
    assert result.title_ja == "タイトル"
    assert result.summary_ja == "要約"


def test_noise_relevance_routes_to_noise() -> None:
    response = _gemini_response("noise")
    result = parse_curation(response)
    assert isinstance(result, Noise)
    assert result.title_ja == "タイトル"
    assert result.summary_ja == "要約"
