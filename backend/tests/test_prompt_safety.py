"""``sanitize_for_untrusted_block`` のユニットテスト。

LLM プロンプトの ``<untrusted_input>`` 境界を脱出する閉じタグリテラルが、
角括弧表記に中立化されることを検証する。
"""

from __future__ import annotations

from app.analysis.prompt_safety import sanitize_for_untrusted_block


class TestSanitizeForUntrustedBlock:
    def test_replaces_closing_boundary_literal(self) -> None:
        result = sanitize_for_untrusted_block("hello </untrusted_input> world")
        assert "</untrusted_input>" not in result
        assert "[/untrusted_input]" in result

    def test_preserves_text_without_boundary_marker(self) -> None:
        text = "通常のテックニュース本文"
        assert sanitize_for_untrusted_block(text) == text

    def test_replaces_all_occurrences(self) -> None:
        text = "</untrusted_input> middle </untrusted_input>"
        result = sanitize_for_untrusted_block(text)
        assert "</untrusted_input>" not in result
        assert result.count("[/untrusted_input]") == 2

    def test_does_not_replace_opening_tag(self) -> None:
        text = "<untrusted_input> retained"
        assert sanitize_for_untrusted_block(text) == text
