"""文字列中の認証情報と指定された禁止値を置換する。

パターン集は deployment-log-policy の S1 (パスワード・秘密鍵) / S2 (認証・セッション
トークン) に対応し、ログ出力用の正本としてここが所有する。完全検出は保証せず、
既知形式を隠しつつ通常テキスト (host / ARN / `completion_tokens` 等) は変えない。
"""

from __future__ import annotations

import re

from app.log_policy.rules import BASE_DENY, CREDENTIAL_KEYS, normalize_key

TEXT_LIMIT = 500

# キー付き値を先に除外し、残った裸の認証情報にだけ既知形式を適用する。
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AI provider keys
    (re.compile(r"AIza[A-Za-z0-9_\-]{35}"), "AIza***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***"),
    (re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"), "sk-***"),
    (re.compile(r"tvly-[A-Za-z0-9_\-]{20,}"), "tvly-***"),
    (re.compile(r"pylf_v1_[a-z]{2}_[A-Za-z0-9]+"), "pylf_***"),
    # GitHub PAT-class
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"), "gh*_***"),
    # AWS access key ID (長期 AKIA / 一時 ASIA)
    (re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), "AKIA***"),
    # SigV4 署名付き query / RDS IAM token の認証部分
    (
        re.compile(
            r"(X-Amz-(?:Signature|Credential|Security-Token))=[^&\s'\"]+",
            re.IGNORECASE,
        ),
        r"\1=***",
    ),
    # URL userinfo (driver 付き DSN / rediss / proxy 認証 URL を含む全 scheme)
    (
        re.compile(r"([a-z][a-z0-9+.\-]*)://[^@/\s]+@", re.IGNORECASE),
        r"\1://***@",
    ),
    # JWT (3-segment base64url)
    (
        re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        "eyJ***",
    ),
]


# 引用符付きの値は閉じ忘れも末尾まで伏せ、エスケープされた引用符で切らない。
_ASSIGNMENT = re.compile(r"(?<![\w-])(?P<key>[A-Za-z_][A-Za-z0-9_-]*)['\"]?\s*[:=]\s*")
_QUOTED_VALUE = re.compile(
    r"\"(?:\\[\s\S]?|[^\"\\])*(?:\"|$)|'(?:\\[\s\S]?|[^'\\])*(?:'|$)"
)
_UNQUOTED_VALUE = re.compile(r"[^\s,}&;]+")
_HEADER_VALUE = re.compile(r"[^\r\n}]+")
_HEADER_KEYS = frozenset(
    {"authorization", "proxy_authorization", "cookie", "set_cookie"}
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----|$)",
    re.DOTALL,
)


def _redact_assignments(text: str, keys: frozenset[str]) -> str:
    parts: list[str] = []
    end = 0
    for match in _ASSIGNMENT.finditer(text):
        if match.start() < end:
            continue
        key = normalize_key(match["key"])
        if key not in keys:
            continue
        start = match.end()
        pattern = _HEADER_VALUE if key in _HEADER_KEYS else _UNQUOTED_VALUE
        value = _QUOTED_VALUE.match(text, start) or pattern.match(text, start)
        if value is None:
            continue
        parts.extend((text[end:start], "***"))
        end = value.end()
    parts.append(text[end:])
    return "".join(parts)


def redact_credentials(text: str) -> str:
    """既知の認証情報形式を全体置換し、通常の原因文を残す。"""
    text = _PRIVATE_KEY.sub("***", text)
    text = _redact_assignments(text, CREDENTIAL_KEYS)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_text(text: str, deny: frozenset[str] = BASE_DENY) -> str:
    """識別できる禁止値を秘匿し、文字数制限は呼び出し側で行う。"""
    return _redact_assignments(redact_credentials(text), BASE_DENY | deny)
