"""Crossref Works API の Reader (HTTP 取得 + item→Entry 抽出)。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
import structlog

from app.collection.article_collection.errors import UnreadableResponseError
from app.collection.article_collection.tools.http_error_translation import (
    translate_fetch_exception,
)
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)

# Crossref polite pool 降格防止のため User-Agent に mailto: が必須。
_USER_AGENT = (
    "Mozilla/5.0 (compatible; Vector/1.0; "
    "+https://github.com/yook11/Vector; mailto:crossref-contact@example.invalid)"
)
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# JATS prefix (<jats:p>) と HTML tag を一括で剥がす
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
# Crossref 仕様: published が無ければ online/print/issued の順に fallback。
_DATE_KEYS = ("published", "published-online", "published-print", "issued")


@dataclass(frozen=True, slots=True)
class CrossrefEntry:
    """Crossref Works API の 1 item を写した Entry。

    parse/decode 済みだが意味づけ前の lossless な箱 (記述用・invariant 無し)。
    ``title`` / ``body`` は JATS/HTML markup を剥がし空白正規化済 (decode は
    Reader の責務)。ドメイン上限 (title 字数等) はここで掛けない (converter
    所有)。``license_urls`` は raw な license URL の列で、CC BY 4.0 か否かの
    判定は持たない (収集スコープは Source ``is_collectable_mdpi_work`` の責務)。
    """

    entry_type: str | None
    title: str
    doi: str | None
    body: str
    published: datetime | None
    license_urls: tuple[str, ...]


def _strip_jats(s: str) -> str:
    """JATS XML markup (``<jats:p>`` 等) と HTML タグを剥がし空白正規化。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", s)).strip()


def _parse_published(item: dict[str, Any]) -> datetime | None:
    """``item[key]["date-parts"][0]`` を UTC datetime に。

    Crossref 仕様により ``[YYYY]`` / ``[YYYY, M]`` / ``[YYYY, M, D]`` の
    いずれか。年/月のみは月=1 / 日=1 で補完。``published`` 不在時は
    ``published-online`` / ``published-print`` / ``issued`` を順に fallback。
    """
    for key in _DATE_KEYS:
        block = item.get(key)
        if not isinstance(block, dict):
            continue
        parts_list = block.get("date-parts")
        if not isinstance(parts_list, list) or not parts_list:
            continue
        first = parts_list[0]
        if not isinstance(first, list) or not first:
            continue
        try:
            year = int(first[0])
            month = int(first[1]) if len(first) >= 2 else 1
            day = int(first[2]) if len(first) >= 3 else 1
            return datetime(year, month, day, tzinfo=UTC)
        except (TypeError, ValueError):
            continue
    return None


def _extract_doi(item: dict[str, Any]) -> str | None:
    """``item["DOI"]`` を返す (型 / 空 guard 付き)。"""
    raw = item.get("DOI")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _extract_title(item: dict[str, Any]) -> str:
    """``item["title"]`` は list[str] (Crossref 仕様)、先頭要素を採用し平文化。"""
    raw = item.get("title")
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, str):
            return _strip_jats(first)
    if isinstance(raw, str):
        return _strip_jats(raw)
    return ""


def _extract_license_urls(item: dict[str, Any]) -> tuple[str, ...]:
    """``item["license"][i]["URL"]`` を lossless に列挙 (判定はしない)。"""
    licenses = item.get("license")
    if not isinstance(licenses, list):
        return ()
    urls = [
        lic["URL"]
        for lic in licenses
        if isinstance(lic, dict) and isinstance(lic.get("URL"), str)
    ]
    return tuple(urls)


def normalize_item(item: dict[str, Any]) -> CrossrefEntry:
    """1 Crossref work item を ``CrossrefEntry`` に写す (lossless・no-drop)。"""
    return CrossrefEntry(
        entry_type=item.get("type") if isinstance(item.get("type"), str) else None,
        title=_extract_title(item),
        doi=_extract_doi(item),
        body=_strip_jats(item.get("abstract") or ""),
        published=_parse_published(item),
        license_urls=_extract_license_urls(item),
    )


class CrossrefReader:
    """Crossref Works API Reader。"""

    DEFAULT_ENDPOINT: ClassVar[str] = "https://api.crossref.org/works"

    def __init__(self, *, endpoint_url: str = DEFAULT_ENDPOINT) -> None:
        self._endpoint_url = endpoint_url

    async def fetch_works(
        self,
        *,
        source_name: str,
        issn: str,
        from_pub_date: str,
        rows: int,
    ) -> list[CrossrefEntry]:
        """per-ISSN + ``from-pub-date`` で新着順に recent works を取得。

        Raises:
            ExternalFetchError: HTTP status / transport / SSRF 例外の写像。
        """
        params: dict[str, str | int] = {
            "filter": f"issn:{issn},from-pub-date:{from_pub_date}",
            "rows": rows,
            "sort": "published",
            "order": "desc",
        }

        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(self._endpoint_url, params=params)
                response.raise_for_status()
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                HostBlockedError,
                HostResolutionError,
            ) as e:
                raise translate_fetch_exception(e, source_name=source_name) from e

            try:
                data = response.json()
            except json.JSONDecodeError as e:
                raise UnreadableResponseError(
                    f"crossref json decode error: {source_name}: {e}"
                ) from e

        # envelope shape を確定してから抽出 (接続成功でも構造化できなければ
        # read 失敗。absent key は寛容に空へ、present だが型違いは unreadable)。
        if not isinstance(data, dict):
            raise UnreadableResponseError(
                f"crossref envelope shape error: {source_name}"
            )
        message = data.get("message", {})
        if not isinstance(message, dict):
            raise UnreadableResponseError(
                f"crossref envelope shape error: {source_name}"
            )
        items_raw = message.get("items", [])
        if not isinstance(items_raw, list):
            raise UnreadableResponseError(
                f"crossref envelope shape error: {source_name}"
            )
        items: list[dict[str, Any]] = items_raw
        if not items:
            logger.info("crossref_no_new_items", source=source_name)
        return [normalize_item(item) for item in items]
