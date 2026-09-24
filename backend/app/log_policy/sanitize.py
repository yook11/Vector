"""文字列の内容から既知形式の認証情報を検出して置換する。

パターン集は deployment-log-policy の S1 (パスワード・秘密鍵) / S2 (認証・セッション
トークン) に対応し、ログ出力用の正本としてここが所有する。完全検出は保証せず、
既知形式を隠しつつ通常テキスト (host / ARN / `completion_tokens` 等) は変えない。
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable

_PROVIDER_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AI provider keys
    (re.compile(r"AIza[A-Za-z0-9_\-]{35}"), "AIza***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***"),
    (re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"), "sk-***"),
    (re.compile(r"tvly-[A-Za-z0-9_\-]{20,}"), "tvly-***"),
    (re.compile(r"pylf_v1_[a-z]{2}_[A-Za-z0-9]+"), "pylf_***"),
    # GitHub PAT-class
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"), "gh*_***"),
]

_AWS_ACCESS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
_AWS_SIGNED_QUERY_CREDENTIAL = re.compile(
    r"(X-Amz-(?:Signature|Credential|Security-Token))=[^&\s'\"]+",
    re.IGNORECASE,
)
# scheme から探すと長い英字列で戻り読みするため、:// を先に取り左を確認する。
_URL_USERINFO = re.compile(r"://[^@/\s]+@")
# 旧IGNORECASEが認識したUnicodeの4文字も秘匿範囲に残す。
_SCHEME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZİıſK")
_SCHEME_CHARS = _SCHEME_START | frozenset("0123456789+.-")
_JWT_SEGMENT = re.compile(r"[A-Za-z0-9_\-]+")


_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----|$)",
    re.DOTALL,
)


def sanitize_aws_access_key_ids(text: str) -> str:
    """既知形式の AKIA / ASIA アクセスキーIDを AKIA*** に置換し、周囲の文を残す。"""
    return _AWS_ACCESS_KEY_ID.sub("AKIA***", text)


def sanitize_aws_signed_query_credentials(text: str) -> str:
    """SigV4の署名・認証スコープ・トークンを伏せ、クエリ名と接続情報を残す。"""
    return _AWS_SIGNED_QUERY_CREDENTIAL.sub(r"\1=***", text)


def sanitize_url_userinfo(text: str) -> str:
    """URL の userinfo 全体を *** に置換し、scheme と接続先を残す。"""
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
        parts.extend((text[end:scheme_start], text[scheme_start:mark], "://***@"))
        end = match.end()
    parts.append(text[end:])
    return "".join(parts)


def sanitize_jwts(text: str) -> str:
    """先頭2区画が eyJ で始まる3区画のJWT形式を、前後の文を残して eyJ*** に置換する。"""
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
        parts.extend((text[end:start], "eyJ***"))
        end = signature.end()
        segments.clear()
    parts.append(text[end:])
    return "".join(parts)


def sanitize_private_key_blocks(text: str) -> str:
    """PEM秘密鍵をブロック全体で伏せ、前後の文を残す。"""
    return _PRIVATE_KEY.sub("***", text)


def sanitize_provider_keys(text: str) -> str:
    """既知のproviderキー形式を置換し、周囲の文を残す。"""
    for pattern, replacement in _PROVIDER_KEY_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_text(text: str) -> str:
    """文字列の内容から秘密情報を検出し、該当部分を伏せる。"""
    text = sanitize_private_key_blocks(text)
    text = sanitize_provider_keys(text)
    text = sanitize_aws_access_key_ids(text)
    text = sanitize_aws_signed_query_credentials(text)
    text = sanitize_url_userinfo(text)
    return sanitize_jwts(text)


_FIELD_SANITIZERS: dict[str, Callable[[str], str]] = {
    "connection_url": sanitize_url_userinfo,
    "upstream_message": sanitize_jwts,
}


def sanitize_field_value(field_name: str, value: object) -> str:
    """対応表にある項目の値を処理し、入力型が合わなければ固定マーカーを返す。"""
    sanitizer = _FIELD_SANITIZERS[field_name]
    if type(value) is not str:
        return "[unsupported]"
    return sanitizer(value)
