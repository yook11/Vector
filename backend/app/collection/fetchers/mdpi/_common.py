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

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.fetchers.tools.crossref_client import CrossrefApiClient
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.shared.security.safe_http import make_safe_async_client
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

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


class BaseMDPICrossrefFetcher:
    """MDPI の Crossref API 経路 Pattern R 共通基底。

    subclass は次の 3 つの ClassVar を必須で差し替える:

    - ``NAME``: ``news_sources.name`` 一致 (``"MDPI Materials"`` 等)
    - ``ISSN``: per-journal ISSN (``"1996-1944"`` 等)
    - ``JOURNAL_NAME``: human readable 名 (``"Materials"`` 等、内部の構造化ログ用)

    type / license / title / body / published_at / DOI の品質ゲート未達は
    yield しない (Outcome 純化原則)。
    """

    NAME: ClassVar[str]
    ISSN: ClassVar[str]
    JOURNAL_NAME: ClassVar[str]
    LANGUAGE: ClassVar[str] = "en"
    ENDPOINT_URL: ClassVar[str] = "https://api.crossref.org/works"
    LOOKBACK_DAYS: ClassVar[int] = 7
    ROWS_PER_REQUEST: ClassVar[int] = 20

    async def fetch(self, source_id: int) -> AsyncIterator[ReadyForArticle]:
        items = await self._fetch_recent_works()
        for item in items:
            converted = self._convert_record(item, source_id)
            if converted is not None:
                yield converted

    async def _fetch_recent_works(self) -> list[dict[str, Any]]:
        """Crossref API から ``LOOKBACK_DAYS`` 内の per-ISSN works を取得する。

        Raises:
            PermanentFetchError: 403 / 404 / 410 / 451 / SSRF host 拒否。
            TemporaryFetchError: 429 / 5xx / タイムアウト / DNS 一時失敗。
        """
        since = (
            (datetime.now(UTC) - timedelta(days=self.LOOKBACK_DAYS)).date().isoformat()
        )
        params: dict[str, str | int] = {
            "filter": f"issn:{self.ISSN},from-pub-date:{since}",
            "rows": self.ROWS_PER_REQUEST,
            "sort": "published",
            "order": "desc",
        }

        async with make_safe_async_client(
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            verify=True,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            try:
                response = await client.get(self.ENDPOINT_URL, params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {self.NAME}") from e
                raise TemporaryFetchError(f"HTTP {status}: {self.NAME}") from e
            except httpx.RequestError as e:
                raise TemporaryFetchError(f"request error: {self.NAME}: {e}") from e
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e

            data = response.json()

        items: list[dict[str, Any]] = list(data.get("message", {}).get("items", []))
        if not items:
            logger.info("mdpi_crossref_no_new_items", source=self.NAME)
        return items

    def _convert_record(
        self,
        item: dict[str, Any],
        source_id: int,
    ) -> ReadyForArticle | None:
        """1 Crossref record を ``ReadyForArticle`` に変換する純関数。"""
        if item.get("type") != "journal-article":
            return None

        if not _validate_license(item):
            return None

        title = _extract_title(item)
        if not title:
            return None
        title = title[:_TITLE_MAX_LENGTH]

        body = _strip_jats(item.get("abstract") or "")
        if len(body) < 50:
            return None

        published_at = _parse_date_parts(item)
        if published_at is None:
            return None

        doi = _extract_doi(item)
        if doi is None:
            return None

        try:
            source_url = CanonicalArticleUrl(f"https://doi.org/{doi}")
        except ValueError:
            return None

        try:
            return ReadyForArticle(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            return None


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
