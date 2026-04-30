"""テキスト正規化道具 — HTML 平文化と空白整形。

collection-acquisition-redesign Phase 0c。RSS の ``<content:encoded>`` から
そのまま記事本文を取り出すソース (例: VentureBeat) のために、HTML タグを
段落改行付きで平文化する関数を提供する。

設計判断:

- ``<br>`` → ``\n``、``</p>`` → ``\n\n`` のみ機械変換し、それ以外のタグは
  ``app.utils.sanitize.normalize_text`` の既存ロジック (タグ除去 + HTML
  エンティティデコード + NFKC + C0/C1 制御文字除去 + strip) に委ねる。
- AI 抽出結果 (Stage 2) のような文字レベル整形は行わない: ここは Fetcher 出口
  なので casing 等は触らない (memory ``feedback_ai_extraction_casing`` の
  方針: 永続化前の機械的整形は NFKC + 空白に絞る)。
"""

from __future__ import annotations

import re

from app.utils.sanitize import normalize_text

_BR_RE = re.compile(r"<br\s*/?\s*>", re.IGNORECASE)
_CLOSING_P_RE = re.compile(r"</\s*p\s*>", re.IGNORECASE)


def html_to_plain_text(html: str) -> str:
    """HTML を段落改行付きで平文化する。

    変換手順:
    1. ``<br>`` 系 → ``\n``
    2. ``</p>`` → ``\n\n``
    3. ``normalize_text`` に委譲: 残タグ除去 + HTML エンティティデコード
       + NFKC + C0/C1 制御文字除去 (タブ・改行は保持) + 前後空白除去
    """
    converted = _BR_RE.sub("\n", html)
    converted = _CLOSING_P_RE.sub("\n\n", converted)
    return normalize_text(converted)
