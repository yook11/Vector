"""extract_first_sentence の正常系 / 句点なし / 空文字テスト。"""

from __future__ import annotations

import pytest

from app.insights.briefing.domain.headline import extract_first_sentence


class TestExtractFirstSentence:
    def test_returns_first_sentence_with_period(self) -> None:
        assert extract_first_sentence("これは第1文。これは第2文。") == "これは第1文。"

    def test_returns_whole_string_when_no_period(self) -> None:
        assert extract_first_sentence("句点なしの文字列") == "句点なしの文字列"

    def test_returns_empty_for_empty_input(self) -> None:
        assert extract_first_sentence("") == ""

    def test_returns_only_period_when_starts_with_period(self) -> None:
        assert extract_first_sentence("。残り") == "。"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("単一文。", "単一文。"),
            ("a。b。c。", "a。"),
            ("English text.", "English text."),
        ],
    )
    def test_parametrized(self, raw: str, expected: str) -> None:
        assert extract_first_sentence(raw) == expected
