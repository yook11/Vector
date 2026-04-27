"""LLM プロンプトの ``<untrusted_input>`` 境界マーカを保護するユーティリティ。

外部由来テキストを LLM プロンプトの境界ブロック内に埋め込むとき、入力テキスト
中に閉じタグリテラルが含まれていると LLM がブロックを抜けたと解釈する余地が
残る。本モジュールはその脱出を構造的に防ぐ。
"""

from __future__ import annotations

_BOUNDARY_CLOSE = "</untrusted_input>"
_NEUTRALIZED = "[/untrusted_input]"


def sanitize_for_untrusted_block(text: str) -> str:
    """``</untrusted_input>`` リテラルを角括弧表記に置換し境界脱出を防ぐ。

    山括弧表記を角括弧表記に置き換えることで、LLM が境界マーカを脱出したと誤認
    するのを防ぐ。入力テキストの可読性は維持され、LLM の理解度も損なわない。
    """
    return text.replace(_BOUNDARY_CLOSE, _NEUTRALIZED)
