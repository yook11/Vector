"""Briefing 一覧で表示する headline 第1文抽出。

newspaper 風のプレビュー用。`。` (日本語句点) で分割し第1文 + 句点を返す。
truncate / ellipsis は責務外 (UI 上の clamp で表現する)。
"""

from __future__ import annotations


def extract_first_sentence(headline: str) -> str:
    """日本語句点 ``。`` で分割した第1文 (句点込み) を返す。

    句点が含まれない場合は ``headline`` 全体を返す。
    """
    head, sep, _ = headline.partition("。")
    return head + sep if sep else head
