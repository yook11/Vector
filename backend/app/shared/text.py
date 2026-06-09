"""共有テキスト正規化ヘルパー。

`time.py` (時刻 helper) と並列で、BC 横断の technical primitive を一か所に集約する。
VO ではなく純粋関数として提供し、各 BC の VO (Pydantic field_validator) から呼び出す
実装ヘルパーとして使う。
"""

from __future__ import annotations

import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """ドメイン境界で AI 出力テキストを正規化する。

    HTML タグ除去 + HTML エンティティデコード + Unicode NFKC 正規化 +
    C0/C1 制御文字除去 (タブ・改行は保持) + 前後空白除去。

    用途: Stage 3/4 (Curation/Assessment) ドメインの VO 境界で title_ja /
    summary_ja / investor_take / surface / description などを sanitize する。
    NFKC 正規化と制御文字除去を含むため、AI 応答経由の混入を防ぐ。
    """
    cleaned = _TAG_RE.sub("", text)
    cleaned = html.unescape(cleaned)
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = "".join(
        ch for ch in cleaned if unicodedata.category(ch) != "Cc" or ch in "\t\n"
    )
    return cleaned.strip()


def normalize_mention_surface(text: str) -> str:
    """mention surface 専用の正規化 (``normalize_text`` + 連続空白の畳み込み)。

    surface は trend 集計で ``lower(collapse(surface))`` の名寄せキーになるため、
    読取側 (SQL / ``MentionName`` VO) が連続空白・タブ・改行を単一空白へ畳むのに
    合わせて書込側でも畳む。語境界の単一空白は名寄せに必要なため残す
    (``Open AI`` と ``OpenAI`` は別物)。``normalize_text`` 本体は改行を保持する
    必要のある summary / investor_take に使うため触らない。
    """
    return _WHITESPACE_RUN.sub(" ", normalize_text(text)).strip()
