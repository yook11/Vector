"""MDPI 4 journal の Crossref API 経路 Fetcher 共通基底 (Phase 3 PR 3-c-4)。

MDPI は ``https://www.mdpi.com/<ISSN>/feed`` の RSS を提供するが、Cloudflare WAF
で 4 ISSN 全 403 となり常時 block される (2026-05-04 PoC 確認済)。OAI-PMH
``https://oai.mdpi.com/oai/oai2.php`` は 200 OK だが setSpec が article-type 別
のみで per-journal/ISSN フィルタができないため不採用。

代わりに Crossref API ``https://api.crossref.org/works`` の per-ISSN filter 経路
を採用する (4 ISSN 全 200 OK + abstract 800-2000 chars + license CC BY 4.0 +
DOI 直接取得を PoC で確認)。

per-source 設計:

- **Pattern R** via ``abstract``: 800-2000 chars の JATS XML 形式、``_strip_jats``
  で ``<jats:p>`` 含む markup を剥がす
- **type filter**: ``item["type"] == "journal-article"`` のみ accept、corrections
  / editorials は drop
- **license gate**: ``license[i]["URL"]`` のいずれかが CC BY 4.0 URL を含まない
  → drop (MDPI は uniform CC BY 4.0 だが念のため paranoid gate)
- **source_url**: ``https://doi.org/<DOI>`` (canonical resolver、publisher 別
  landing でなく DOI URL)
- **polite pool**: User-Agent に ``mailto:`` 必須 (Crossref polite pool 降格防止)
- **date filter**: ``from-pub-date`` で rolling ``LOOKBACK_DAYS`` 日窓、cron 周期
  と整合させて初回投入時 backfill を防ぐ
- ``rows=ROWS_PER_REQUEST`` / ``sort=published`` / ``order=desc`` で新着優先
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
import structlog

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.crossref_client import CrossrefApiClient
from app.collection.fetchers.tools.fetched_article import FetchedArticle

logger = structlog.get_logger(__name__)

# Crossref polite pool 降格防止のため User-Agent に mailto: が必須。
_USER_AGENT = (
    "Mozilla/5.0 (compatible; Vector/1.0; "
    "+https://github.com/yook11/Vector; mailto:crossref-contact@example.invalid)"
)
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_CC_BY_4_URL_RE = re.compile(r"creativecommons\.org/licenses/by/4\.0", re.IGNORECASE)
# JATS prefix (<jats:p>) と HTML tag を一括で剥がす
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_TITLE_MAX_LENGTH = 500


def _strip_jats(s: str) -> str:
    """JATS XML markup (``<jats:p>`` 等) と HTML タグを剥がし空白正規化。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", s)).strip()


def _parse_date_parts(item: dict[str, Any]) -> PublishedAt | None:
    """``item["published"]["date-parts"][0]`` を UTC ``PublishedAt`` に変換。

    Crossref 仕様により ``[YYYY]`` / ``[YYYY, M]`` / ``[YYYY, M, D]`` のいずれか。
    年/月のみの場合は月=1 / 日=1 で補完する。``published`` が無い場合は
    ``published-online`` / ``published-print`` / ``issued`` を順に fallback。
    """
    for key in ("published", "published-online", "published-print", "issued"):
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
            dt = datetime(year, month, day, tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        return PublishedAt(value=dt)
    return None


def _validate_license(item: dict[str, Any]) -> bool:
    """``item["license"][i]["URL"]`` のいずれかが CC BY 4.0 URL を含めば True。"""
    licenses = item.get("license")
    if not isinstance(licenses, list):
        return False
    for lic in licenses:
        if not isinstance(lic, dict):
            continue
        url = lic.get("URL")
        if isinstance(url, str) and _CC_BY_4_URL_RE.search(url):
            return True
    return False


def _extract_doi(item: dict[str, Any]) -> str | None:
    """``item["DOI"]`` を返す (型 / 空 guard 付き)。"""
    raw = item.get("DOI")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _extract_title(item: dict[str, Any]) -> str:
    """``item["title"]`` は list[str] (Crossref 仕様)、先頭要素を採用。"""
    raw = item.get("title")
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, str):
            return _strip_jats(first)
    if isinstance(raw, str):
        return _strip_jats(raw)
    return ""


class BaseMDPICrossrefAdapter:
    """MDPI 4 journal の Crossref API 経路 ``SourceAdapter`` 共通基底 (Pattern R)。

    HTTP 取得 + per-ISSN filter + sort/order 構築は ``CrossrefApiClient`` に
    委譲する。Adapter は item ごとの type/license/title/abstract/date/DOI
    判定だけを担う (旧 ``BaseMDPICrossrefFetcher._convert_record`` の判定順を
    完全踏襲)。

    subclass は ``NAME`` / ``ISSN`` / ``JOURNAL_NAME`` の 3 ClassVar を必須で
    差し替える (P3c の Djangoplicity / Frontiers と同じ thin subclass 形)。
    """

    NAME: ClassVar[str]
    ISSN: ClassVar[str]
    JOURNAL_NAME: ClassVar[str]
    LANGUAGE: ClassVar[str] = "en"
    ENDPOINT_URL: ClassVar[str] = "https://api.crossref.org/works"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE
    LOOKBACK_DAYS: ClassVar[int] = 7
    ROWS_PER_REQUEST: ClassVar[int] = 20

    def __init__(self, client: CrossrefApiClient | None = None) -> None:
        self._client = client or CrossrefApiClient()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        from_pub_date = (
            (datetime.now(UTC) - timedelta(days=self.LOOKBACK_DAYS)).date().isoformat()
        )
        items = await self._client.works(
            source_name=self.NAME,
            issn=self.ISSN,
            from_pub_date=from_pub_date,
            rows=self.ROWS_PER_REQUEST,
        )
        # 判定順は旧 _convert_record (本ファイル冒頭) と完全一致:
        # type → license → title → body → date → DOI.
        for item in items:
            if item.get("type") != "journal-article":
                continue  # business: corrections/editorials drop
            if not _validate_license(item):
                continue  # business: CC BY 4.0 のみ
            title = _extract_title(item)
            if not title:
                continue
            title = title[:_TITLE_MAX_LENGTH]
            body = _strip_jats(item.get("abstract") or "")
            if len(body) < 50:
                continue  # business: 短い abstract は信用しない
            published = _parse_date_parts(item)
            if published is None:
                continue
            doi = _extract_doi(item)
            if doi is None:
                continue
            yield FetchedArticle(
                title=title,
                url=f"https://doi.org/{doi}",
                body=body,
                published_at=published.value,
            )
