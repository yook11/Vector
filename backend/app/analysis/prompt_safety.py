"""LLM プロンプトの ``<untrusted_input>`` 境界マーカを保護するユーティリティ。

外部由来テキストを LLM プロンプトの境界ブロック内に埋め込むとき、入力テキスト
中に境界タグや section 風見出しが含まれていると、LLM がブロックを抜けて別
セクションを開始したと誤認する余地が残る。本モジュールはその脱出経路を
構造的に塞ぐ。

無害化対象:
    - 閉じタグ ``</untrusted_input>`` (大小文字 / 内部空白バリアント含む)
      -> ``[/untrusted_input]`` (境界脱出防止 / red-team C8 対策)
    - 開きタグ ``<untrusted_input>`` (同) -> ``[untrusted_input]``
    - 行頭 ATX マーカ ``^#{1,6}[ \\t　]+`` -> ``#`` と空白の間に ZWSP 挿入
      (Vector の prompt は ``# Step N`` を section 区切りに使うため、入力に
      このパターンが混ざると LLM が偽の指示セクションとして再解釈する余地
      がある。半角空白だけでなくタブ・全角空白 (U+3000) も捕捉 / red-team C3)
    - 全角括弧 section header ``【...】`` -> ``【...​】`` (閉じ括弧前 ZWSP)
      briefing prompt が ``【ルール】 ``【出力】`` ``【重要性の判断軸】`` を
      section delimiter として使うため、入力に同パターンが混ざると LLM が
      新規セクションとして解釈する余地がある (red-team C3 / F7)

設計方針:
    過剰サニタイズによる本文情報損失を避けるため、Vector の現プロンプト構造で
    実害根拠のある攻撃ベクタのみを対象にする。代替境界マーカ (``---``, ``===``,
    ``` ``` ``) と JSON injection は Vector の prompt 構造で section 区切りとして
    機能しないため対象外。削除でなく ZWSP 挿入で「LLM が機械的に section
    delimiter として parse しない」状態に弱める方針 (人間可読性は保つ)。
"""

from __future__ import annotations

import re

# boundary tag は IGNORECASE + 内部空白許容で全バリアントを 1 regex で捕捉する。
# 元の string replace では ``</UNTRUSTED_INPUT>`` ``</ untrusted_input >`` 等を
# 素通りさせていた (red-team C8 / F22)。
_BOUNDARY_CLOSE = re.compile(r"<\s*/\s*untrusted_input\s*>", re.IGNORECASE)
_BOUNDARY_CLOSE_NEUTRAL = "[/untrusted_input]"
_BOUNDARY_OPEN = re.compile(r"<\s*untrusted_input\s*>", re.IGNORECASE)
_BOUNDARY_OPEN_NEUTRAL = "[untrusted_input]"

_ZWSP = "​"

# ATX header は ASCII 半角空白 / tab / 全角空白 (U+3000) を捕捉する。
# ``\s`` は改行を含むので multiline 行頭マッチが壊れる。明示的な文字クラスを
# 使う (red-team C3 / F6)。
_ATX_HEADER = re.compile(r"^(#{1,6})[ \t　]+", flags=re.MULTILINE)

# 全角括弧 section header: ``【X】`` を ``【X​】`` (閉じ括弧前に ZWSP) に変換し
# LLM の section header 解釈経路を崩す。中身の長さに上限を設けず、``【` ``】``
# で囲まれた区間を全て対象にする (red-team chain β: 旧 ``{1,20}`` 制限下で
# 21+ 文字の偽 ``【ルール: ...】`` が素通りする bypass が確認されたため)。
# 本文中の引用元注釈等の長い ``【...】`` も ZWSP 入りになるが、ZWSP は人間に
# 見えず LLM の内容理解にも影響しない (red-team C3 / F7)。
_FULLWIDTH_BRACKET_HEADER = re.compile(r"【([^】\n]+)】")


def contains_injection_boundary(text: str) -> bool:
    """untrusted 境界タグ (``<untrusted_input>`` / ``</untrusted_input>`` の全
    バリアント) を含むかを判定する。

    sanitize と違い「無害化」ではなく「検知」が目的。境界タグは正当な記事本文
    にはほぼ出現しない高信号 (near-zero false positive) なので、監査の injection
    検知信号として使える。行頭 ATX ``#`` / 全角括弧 ``【】`` は benign なニュース
    本文 (``# 見出し`` / ``【速報】``) に頻出しノイズ化するため、sanitize では
    無害化しても検知 (本述語) には含めない。
    """
    return bool(_BOUNDARY_OPEN.search(text) or _BOUNDARY_CLOSE.search(text))


def sanitize_for_untrusted_block(text: str) -> str:
    """境界タグ・行頭 ATX マーカ・全角括弧 section header を無害化し、
    LLM 命令層への汚染を防ぐ。

    閉じタグ・開きタグは大小文字や内部空白を含むバリアントを 1 つの正規
    表現で捕捉して角括弧表記に置換し境界誤認を防ぐ。行頭 ATX マーカは
    ``#`` と空白の間に ZWSP を挟み、LLM が Markdown セクションヘッダとして
    解釈する経路を崩す。``【...】`` 風 section header も同様に閉じ括弧前に
    ZWSP を挟む。入力テキストの可読性は損なわず、LLM の理解度も実用上の
    影響はない。
    """
    text = _BOUNDARY_CLOSE.sub(_BOUNDARY_CLOSE_NEUTRAL, text)
    text = _BOUNDARY_OPEN.sub(_BOUNDARY_OPEN_NEUTRAL, text)
    text = _ATX_HEADER.sub(rf"\1{_ZWSP} ", text)
    text = _FULLWIDTH_BRACKET_HEADER.sub(rf"【\1{_ZWSP}】", text)
    return text
