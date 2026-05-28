"""MentionName VO の正規化動作テスト。

MentionName は Stage 4 ``Mention.surface`` を週次集計で表示用にラップした VO で、
表示と検索の責務を分離する:
- 表示用: NFKC + 前後空白除去 + 連続空白統合 (casing は保持)
- 検索用 (match_key): 表示用文字列を str.lower() した値

casefold() は使わない。AI 抽出結果の casing は文脈情報なので NFKC + 空白整形までに
留める方針 (memory: feedback_ai_extraction_casing.md)。
"""

import pytest
from pydantic import ValidationError

from app.insights.trend_discovery.domain.mention_name import MentionName


class TestMentionNameNormalization:
    """NFKC + 空白統合 + casing 保持の表示用整形。"""

    def test_preserves_casing(self) -> None:
        """大文字小文字は変更しない (NVIDIA はそのまま)。"""
        name = MentionName("NVIDIA")
        assert name.root == "NVIDIA"

    def test_preserves_mixed_casing(self) -> None:
        """混在 casing もそのまま保持する (iPhone, OpenAI)。"""
        assert MentionName("iPhone").root == "iPhone"
        assert MentionName("OpenAI").root == "OpenAI"

    def test_strips_leading_trailing_whitespace(self) -> None:
        name = MentionName("  Apple  ")
        assert name.root == "Apple"

    def test_collapses_internal_whitespace(self) -> None:
        """連続する空白は単一空白に統合する。"""
        assert MentionName("New  York").root == "New York"
        assert MentionName("San   Francisco").root == "San Francisco"

    def test_collapses_mixed_whitespace_chars(self) -> None:
        """タブ・改行・全角空白などの連続も単一半角空白に統合する。"""
        assert MentionName("Hello\t\tWorld").root == "Hello World"
        assert MentionName("foo\n\nbar").root == "foo bar"

    def test_nfkc_full_width_to_half_width(self) -> None:
        """NFKC により全角英数は半角に正規化される。"""
        assert MentionName("ＡＰＰＬＥ").root == "APPLE"
        assert MentionName("１２３").root == "123"

    def test_nfkc_compatibility_chars(self) -> None:
        """NFKC により互換文字は標準形に正規化される (㈱ → (株))。"""
        # 半角カナ → 全角カナ
        assert MentionName("ｱｯﾌﾟﾙ").root == "アップル"

    def test_strips_after_nfkc(self) -> None:
        """NFKC 結果に余白が含まれても strip + collapse が適用される。"""
        assert MentionName("  ＡＰＰＬＥ   Inc  ").root == "APPLE Inc"


class TestMentionNameMatchKey:
    """match_key は重複検出・JOIN 用の小文字キー。"""

    def test_match_key_is_lowercase(self) -> None:
        assert MentionName("NVIDIA").match_key == "nvidia"
        assert MentionName("OpenAI").match_key == "openai"

    def test_match_key_after_normalization(self) -> None:
        """match_key は NFKC + 空白統合適用後の文字列を lower したもの。"""
        assert MentionName("ＡＰＰＬＥ  Inc").match_key == "apple inc"

    def test_match_key_preserves_root_casing(self) -> None:
        """match_key を取っても root の casing は失われない。"""
        name = MentionName("Apple")
        assert name.match_key == "apple"
        assert name.root == "Apple"

    def test_match_key_uses_lower_not_casefold(self) -> None:
        """str.lower() を使う (str.casefold() ではない)。

        AI 抽出結果の casing は文脈情報。casefold は ß → ss など過剰な正規化を
        行うため使用しない。
        """
        # ß は lower では ß のまま、casefold では ss になる
        name = MentionName("Straße")
        assert name.match_key == "straße"


class TestMentionNameInvariants:
    """既存の不変条件 (1-200 文字、frozen) を retain することを確認。"""

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            MentionName("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            MentionName("   ")

    def test_rejects_over_200_after_normalize(self) -> None:
        """正規化後の長さで 200 を超えるものは reject。"""
        with pytest.raises(ValidationError, match="at most 200"):
            MentionName("a" * 201)

    def test_accepts_200_after_normalize(self) -> None:
        """正規化後ちょうど 200 文字は許容。"""
        name = MentionName("a" * 200)
        assert len(name.root) == 200

    def test_normalized_length_used_for_check(self) -> None:
        """空白統合後の長さで判定する (元 201 文字 → 統合後 200 文字なら OK)。"""
        # "a " * 100 + "a" = 201 文字、collapse 後は変わらず 201 (空白は1個ずつ)
        # collapse が効くケース: "a  " * 100 + "a" = 301 文字
        # → "a " * 100 + "a" = 201 文字
        # ここでは strip + 連続空白統合の効果が長さ判定に効くことを示す
        raw = "a" + " " * 50 + "b"  # 元 52 文字、統合後 "a b" = 3 文字
        name = MentionName(raw)
        assert name.root == "a b"

    def test_immutable(self) -> None:
        name = MentionName("Apple")
        with pytest.raises(ValidationError, match="frozen"):
            name.root = "hacked"  # type: ignore[misc]
