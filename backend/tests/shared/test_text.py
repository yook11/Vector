"""共有テキスト正規化ヘルパー ``normalize_text`` / ``normalize_mention_surface``
の振る舞い境界テスト。

``normalize_mention_surface`` は mention surface の名寄せキー専用に連続空白を畳む。
``normalize_text`` は summary / investor_take の改行保持が要件のため畳まない —
両者の差 (改行の扱い) を回帰として固定する。
"""

from __future__ import annotations

import pytest

from app.shared.text import normalize_mention_surface, normalize_text


class TestNormalizeTextPreservesNewlines:
    """``normalize_text`` は改行・タブを保持する (mention 用 collapse と区別)。"""

    def test_preserves_internal_newline(self) -> None:
        assert normalize_text("line1\nline2") == "line1\nline2"

    def test_preserves_internal_tab(self) -> None:
        assert normalize_text("col1\tcol2") == "col1\tcol2"

    def test_does_not_collapse_double_space(self) -> None:
        assert normalize_text("Open  AI") == "Open  AI"


class TestNormalizeMentionSurface:
    """``normalize_mention_surface`` は名寄せキー用に連続空白を単一空白へ畳む。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Open  AI", "Open AI"),
            ("Open\tAI", "Open AI"),
            ("Open\nAI", "Open AI"),
            ("Open \t\n AI", "Open AI"),
            ("  Open AI  ", "Open AI"),
        ],
    )
    def test_collapses_whitespace_runs(self, raw: str, expected: str) -> None:
        assert normalize_mention_surface(raw) == expected

    def test_preserves_single_internal_space(self) -> None:
        # 語境界の単一空白は名寄せに必要 (Open AI ≠ OpenAI)
        assert normalize_mention_surface("Open AI") == "Open AI"

    def test_strips_html_then_collapses(self) -> None:
        # normalize_text のタグ除去 → collapse の順で効くこと
        assert normalize_mention_surface("<b>NV  IDIA</b>") == "NV IDIA"

    def test_nfkc_then_collapse(self) -> None:
        # 全角空白 (NFKC で ASCII space 化) も畳む
        assert normalize_mention_surface("Open　　AI") == "Open AI"

    def test_empty_after_strip(self) -> None:
        assert normalize_mention_surface("   ") == ""
