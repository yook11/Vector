"""``html_to_plain_text`` のユニットテスト。"""

from __future__ import annotations

from app.collection.article_acquisition.tools.text_normalizer import html_to_plain_text


class TestHtmlToPlainText:
    def test_converts_br_to_newline(self) -> None:
        assert html_to_plain_text("line1<br>line2") == "line1\nline2"

    def test_converts_self_closing_br(self) -> None:
        assert html_to_plain_text("line1<br/>line2") == "line1\nline2"

    def test_converts_br_with_whitespace(self) -> None:
        assert html_to_plain_text("line1<br />line2") == "line1\nline2"

    def test_converts_closing_p_to_double_newline(self) -> None:
        assert html_to_plain_text("<p>para1</p><p>para2</p>") == "para1\n\npara2"

    def test_strips_inline_tags(self) -> None:
        assert html_to_plain_text("Hello <strong>world</strong>") == "Hello world"

    def test_decodes_html_entities(self) -> None:
        assert html_to_plain_text("Tom &amp; Jerry") == "Tom & Jerry"

    def test_normalizes_nfkc(self) -> None:
        # 全角英数字 ('Ａ' = U+FF21) は NFKC で半角 ('A') に正規化される
        assert html_to_plain_text("Ａ") == "A"

    def test_strips_outer_whitespace(self) -> None:
        assert html_to_plain_text("  <p>body</p>  ") == "body"

    def test_handles_mixed_paragraph_and_break(self) -> None:
        result = html_to_plain_text("<p>line1<br>line2</p><p>para2</p>")
        assert result == "line1\nline2\n\npara2"
