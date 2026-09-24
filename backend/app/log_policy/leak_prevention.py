"""出力する文字列に紛れた既知の種類の認証情報を、種別付きの表記へ置き換える。

allow・deny・mask・サニタイズを通った後に最後にかける補助の保護で、
列挙した種類以外は検出しない。パターン集は deployment-log-policy の
S1 / S2 に対応し、ログ出力用の正本としてここが所有する。
"""

from __future__ import annotations

import re
from collections import deque

from app.log_policy.base import CREDENTIAL_KEYS, normalize_key

_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----|$)",
    re.DOTALL,
)
_GEMINI_API_KEY = re.compile(r"AIza[A-Za-z0-9_\-]{35}")
_DEEPSEEK_API_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[0-9a-f]{32}(?![A-Za-z0-9])")
_LOGFIRE_TOKEN = re.compile(r"pylf_v1_[a-z]{2}_[A-Za-z0-9]+")
_AWS_ACCESS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
# scheme から探すと長い英字列で戻り読みするため、:// を先に取り左を確認する。
_URL_USERINFO = re.compile(r"://[^@/\s]+@")
# 旧IGNORECASEが認識したUnicodeの4文字も、Kと紛れないようエスケープで残す。
_SCHEME_START = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ\u0130\u0131\u017f\u212a"
)
_SCHEME_CHARS = _SCHEME_START | frozenset("0123456789+.-")
_JWT_SEGMENT = re.compile(r"[A-Za-z0-9_\-]+")
_ASSIGNMENT = re.compile(r"(?<![\w-])(?P<key>[A-Za-z_][A-Za-z0-9_-]*)['\"]?\s*[:=]\s*")


def redact_private_key_blocks(text: str) -> str:
    """PEM秘密鍵をブロック全体で置き換え、前後の文を残す。"""
    return _PRIVATE_KEY.sub("[redacted:private_key]", text)


def redact_gemini_api_keys(text: str) -> str:
    """Gemini API キーの形式に一致する部分を置き換え、周囲の文を残す。"""
    return _GEMINI_API_KEY.sub("[redacted:gemini_api_key]", text)


def redact_deepseek_api_keys(text: str) -> str:
    """DeepSeek API キーの形式に一致する部分を置き換え、周囲の文を残す。"""
    return _DEEPSEEK_API_KEY.sub("[redacted:deepseek_api_key]", text)


def redact_logfire_tokens(text: str) -> str:
    """Logfire write token の形式に一致する部分を置き換え、周囲の文を残す。"""
    return _LOGFIRE_TOKEN.sub("[redacted:logfire_token]", text)


def redact_aws_access_key_ids(text: str) -> str:
    """AKIA / ASIA のアクセスキーIDを置き換え、周囲の文を残す。"""
    return _AWS_ACCESS_KEY_ID.sub("[redacted:aws_access_key_id]", text)


def redact_url_userinfo(text: str) -> str:
    """URL の userinfo 全体を置き換え、scheme と接続先を残す。"""
    parts: list[str] = []
    end = 0
    for match in _URL_USERINFO.finditer(text):
        mark = match.start()
        scheme_start = mark
        while scheme_start > end and text[scheme_start - 1] in _SCHEME_CHARS:
            scheme_start -= 1
        while scheme_start < mark and text[scheme_start] not in _SCHEME_START:
            scheme_start += 1
        if scheme_start == mark:
            continue
        parts.extend(
            (
                text[end:scheme_start],
                text[scheme_start:mark],
                "://[redacted:url_userinfo]@",
            )
        )
        end = match.end()
    parts.append(text[end:])
    return "".join(parts)


def redact_jwts(text: str) -> str:
    """先頭2区画が eyJ で始まる3区画のJWT形式を、前後の文を残して置き換える。"""
    if "eyJ" not in text:
        return text
    parts: list[str] = []
    end = 0
    segments: deque[re.Match[str]] = deque(maxlen=3)
    # 区画を一度ずつ走査し、不成立のeyJ候補から長い接尾部を探し直さない。
    for segment in _JWT_SEGMENT.finditer(text):
        if segments and (
            segment.start() != segments[-1].end() + 1 or text[segments[-1].end()] != "."
        ):
            segments.clear()
        segments.append(segment)
        if len(segments) < 3:
            continue
        header, payload, signature = segments
        if payload.end() - payload.start() < 4 or not text.startswith(
            "eyJ", payload.start()
        ):
            continue
        start = text.find("eyJ", header.start(), header.end() - 1)
        if start < 0:
            continue
        parts.extend((text[end:start], "[redacted:jwt]"))
        end = signature.end()
        segments.clear()
    parts.append(text[end:])
    return "".join(parts)


def redact_credential_assignments(text: str) -> str:
    """認証キー名の区切りより後ろを、値の終端を推測せず末尾まで置き換える。"""
    for match in _ASSIGNMENT.finditer(text):
        if normalize_key(match["key"]) not in CREDENTIAL_KEYS:
            continue
        if match.end() == len(text):
            return text
        return text[: match.end()] + "[redacted:credential]"
    return text


def prevent_credential_leaks(text: str) -> str:
    """範囲が形式で決まる検出を先に適用し、最後にキー名の後ろを末尾まで置き換える。"""
    text = redact_private_key_blocks(text)
    text = redact_gemini_api_keys(text)
    text = redact_deepseek_api_keys(text)
    text = redact_logfire_tokens(text)
    text = redact_aws_access_key_ids(text)
    text = redact_url_userinfo(text)
    text = redact_jwts(text)
    return redact_credential_assignments(text)
