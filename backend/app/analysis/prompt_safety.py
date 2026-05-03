"""LLM プロンプトの ``<untrusted_input>`` 境界マーカを保護するユーティリティ。

外部由来テキストを LLM プロンプトの境界ブロック内に埋め込むとき、入力テキスト
中に境界タグや ATX 風セクション見出しが含まれていると、LLM がブロックを抜けて
別セクションを開始したと誤認する余地が残る。本モジュールはその脱出経路を
構造的に塞ぐ。

無害化対象:
    - 閉じタグ ``</untrusted_input>`` -> ``[/untrusted_input]`` (境界脱出防止)
    - 開きタグ ``<untrusted_input>`` -> ``[untrusted_input]`` (二重 open 混乱防止)
    - 行頭 ATX マーカ ``^#{1,6} `` -> ``# `` の間に ZWSP 挿入 (Vector の prompt は
      ``# Step N`` を section 区切りに使うため、入力にこのパターンが混ざると LLM が
      偽の指示セクションとして再解釈する余地がある。削除ではなく ZWSP 挿入で
      「LLM が ATX セクションヘッダと解釈しない」状態に弱める)

設計方針:
    過剰サニタイズによる本文情報損失を避けるため、Vector の現プロンプト構造で
    実害根拠のある攻撃ベクタのみを対象にする。代替境界マーカ (``---``, ``===``,
    ``` ``` ``) と JSON injection は Vector の prompt 構造で section 区切りとして
    機能しないため対象外。
"""

from __future__ import annotations

import re

_BOUNDARY_CLOSE = "</untrusted_input>"
_BOUNDARY_CLOSE_NEUTRAL = "[/untrusted_input]"
_BOUNDARY_OPEN = "<untrusted_input>"
_BOUNDARY_OPEN_NEUTRAL = "[untrusted_input]"
_ZWSP = "​"
_ATX_HEADER = re.compile(r"^(#{1,6}) ", flags=re.MULTILINE)


def sanitize_for_untrusted_block(text: str) -> str:
    """境界タグと行頭 ATX マーカを無害化し、LLM 命令層への汚染を防ぐ。

    閉じタグ・開きタグは角括弧表記に置換し境界誤認を防ぐ。行頭 ATX マーカは
    ``#`` と空白の間に ZWSP を挟み、LLM が Markdown セクションヘッダとして
    解釈する経路を崩す。入力テキストの可読性は損なわず、LLM の理解度も
    実用上の影響はない。
    """
    text = text.replace(_BOUNDARY_CLOSE, _BOUNDARY_CLOSE_NEUTRAL)
    text = text.replace(_BOUNDARY_OPEN, _BOUNDARY_OPEN_NEUTRAL)
    text = _ATX_HEADER.sub(rf"\1{_ZWSP} ", text)
    return text
