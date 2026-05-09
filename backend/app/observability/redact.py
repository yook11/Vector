"""error_message / log payload に secret prefix が混入する経路を伏字化する。

`pipeline_events.payload` への永続化前に ``str(exc)`` に含まれる API key /
Authorization header / DSN credential 等を opaque な ``***`` 系トークンに
置換する best-effort defense-in-depth (red-team chain γ-2)。

完全な検出は不可能なので「既知 pattern を確実に隠す」+「通常テキストを
過剰に変えない」の 2 軸で組む。網羅性を装って false sense of security を
作らない。
"""

from __future__ import annotations

import re

# 順序依存: より specific なパターン (AIza, sk-ant など) を generic な
# Authorization パターンより前に置く (overlap で先勝ち)。
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AI provider keys
    (re.compile(r"AIza[A-Za-z0-9_\-]{35}"), "AIza***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***"),
    # OpenAI: 通常 (sk-...) / project (sk-proj-...) / service account (sk-svcacct-...)
    (re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"), "sk-***"),
    # GitHub PAT-class
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"), "gh*_***"),
    # Authorization header — Python dict repr / HTTP line / log format に
    # 対応する。引用符 / 空白 / `:` `=` 区切りを許容するが、`Authorization-utils`
    # のような単語境界での false positive を防ぐため、必ず `:` `=` を要求する。
    (
        re.compile(
            r"(authorization)\b\s*['\"]?\s*[:=]\s*['\"]?"
            r"(?:Bearer\s+|Basic\s+)?[A-Za-z0-9._\-+/=]{16,}",
            re.IGNORECASE,
        ),
        r"\1: ***",
    ),
    # x-api-key / x-goog-api-key (引用符 / 空白を許容)
    (
        re.compile(
            r"(x-(?:goog-)?api-key)\b\s*['\"]?\s*[:=]\s*['\"]?[^'\"\s,}]{4,}",
            re.IGNORECASE,
        ),
        r"\1: ***",
    ),
    # DSN credential portion (postgres / redis / mysql)
    (
        re.compile(r"(postgres(?:ql)?|redis|mysql)://[^@/\s]+@", re.IGNORECASE),
        r"\1://***@",
    ),
    # JWT (3-segment base64url)
    (
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        "eyJ***",
    ),
]


def redact_secrets(text: str) -> str:
    """既知の secret pattern を ``***`` 系トークンに置換する。

    パターン非該当のテキストは無変化で返す (可読性保持)。
    """
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
