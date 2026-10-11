"""文字列に現れた内部の宛先を、種別付きの表記へ置き換える。

非公開IPの判定は外部通信の宛先方針を正本として使い、ログのためにDNS解決や通信を行わない。
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

from app.http.destination_policy import (
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
)

_SCHEME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_SCHEME_CHARS = _SCHEME_START | frozenset("0123456789+.-")
_URL_END = re.compile(r"[\s'\"<>`]")
_AUTHORITY_END = re.compile(r"[/?#]")
# 区切り(: か .)を含む16進の並びだけを候補にし、通常の単語ごとに判定を呼ばない。
_ADDRESS_CANDIDATE = re.compile(
    r"(?<![0-9A-Za-z:.%])(?=[0-9A-Fa-f]*[:.])[0-9A-Fa-f:.]+(?:%[0-9A-Za-z_.\-]+)?"
)
# 10進・8進・16進の整数だけでできたhostは、クライアントによってIPとして解釈される。
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*")
# IPv6の最長の表記(45文字)にzone IDを足した長さで、超えるものはIPとして扱わない。
_MAX_ADDRESS_LENGTH = 64
_LOCALHOST = re.compile(r"(?<![\w.-])(?:[\w-]+\.)*localhost(?![\w-])", re.IGNORECASE)


def _is_internal_ip(text: str) -> bool | None:
    """非公開IPならTrue、公開IPならFalse、IPとして読めなければNoneを返す。"""
    if len(text) > _MAX_ADDRESS_LENGTH:
        return None
    try:
        PublicIpAddress(text)
    except NotAPublicIpError:
        return True
    except NotAnIpAddressError:
        return None
    return False


def _is_localhost(host: str) -> bool:
    return host == "localhost" or host.endswith(".localhost")


def _url_replacement(url: str) -> str | None:
    """URLのhostが内部を指すか、解釈が分かれる場合だけ置換表記を返す。"""
    # userinfoは認証情報の置換表記を含みうるため、hostの解析から外す。
    authority = _AUTHORITY_END.split(url.partition("://")[2], maxsplit=1)[0]
    try:
        host = urlsplit("//" + authority.rpartition("@")[2]).hostname
    except ValueError:
        return "[redacted:ambiguous_url]"
    if not host:
        return None
    # 送信時の解釈に合わせ、percent-encodingと全角文字を戻してから判定する。
    normalized = unicodedata.normalize("NFKC", unquote(host)).lower().rstrip(".")
    if _is_localhost(normalized):
        return "[redacted:internal_url]"
    match _is_internal_ip(normalized):
        case True:
            return "[redacted:internal_url]"
        case False:
            return None
        case None:
            pass
    if "%" in normalized or _NUMERIC_HOST.fullmatch(normalized):
        return "[redacted:ambiguous_url]"
    return None


def _redact_internal_urls(text: str) -> str:
    """:// から左右へ広げてURLを切り出し、内部を指すURLを全体ごと置き換える。"""
    if "://" not in text:
        return text
    parts: list[str] = []
    end = 0
    search = 0
    while (mark := text.find("://", search)) >= 0:
        url_end = _URL_END.search(text, mark + 3)
        stop = url_end.start() if url_end else len(text)
        search = max(stop, mark + 3)
        start = mark
        while start > end and text[start - 1] in _SCHEME_CHARS:
            start -= 1
        while start < mark and text[start] not in _SCHEME_START:
            start += 1
        if start == mark:
            continue
        replacement = _url_replacement(text[start:stop])
        if replacement is None:
            continue
        parts.extend((text[end:start], replacement))
        end = stop
    parts.append(text[end:])
    return "".join(parts)


def _split_address(candidate: str) -> tuple[str, str]:
    """候補をアドレスと、後ろに続くportや文末の記号に分ける。"""
    if candidate.count(":") == 1:
        host, separator, port = candidate.partition(":")
        return host, separator + port
    address = candidate.rstrip(".:")
    rest = candidate[len(address) :]
    # IPv6の省略表記は :: で終わりうるため、文末の記号と区別する。
    if (
        address
        and rest.startswith("::")
        and _is_internal_ip(address + "::") is not None
    ):
        return address + "::", rest[2:]
    return address, rest


def _redact_address(match: re.Match[str]) -> str:
    candidate = match.group()
    if candidate.count(":") < 2 and candidate.count(".") < 3:
        return candidate
    address, rest = _split_address(candidate)
    if _is_internal_ip(address) is True:
        return "[redacted:internal_ip]" + rest
    return candidate


def redact_internal_destinations(text: str) -> str:
    """URLの宛先を先に判定し、残った文の非公開IPとlocalhostを置き換える。"""
    text = _redact_internal_urls(text)
    text = _ADDRESS_CANDIDATE.sub(_redact_address, text)
    if "localhost" in text.lower():
        text = _LOCALHOST.sub("[redacted:internal_host]", text)
    return text
