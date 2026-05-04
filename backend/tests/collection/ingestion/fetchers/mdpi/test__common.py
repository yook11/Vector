"""``BaseMDPICrossrefFetcher`` (Crossref API Pattern R) の不変条件テスト。

検証する不変条件:

- Crossref item を ``_convert_record`` した結果が永続化 passport の不変条件を満たす
- ``type != "journal-article"`` / ``license`` 欠落 / 必須フィールド欠落は ``Failed``
- HTTP error は ``PermanentFetchError`` / ``TemporaryFetchError`` に分類される
- API request の必須パラメタ (per-ISSN filter / from-pub-date / polite pool UA)
  を必ず付ける
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchedEntry,
    FetchOutcome,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.mdpi._common import (
    BaseMDPICrossrefFetcher,
    _extract_authors,
    _extract_doi,
    _parse_date_parts,
    _strip_jats,
    _validate_license,
)
from app.collection.ingestion.fetchers.mdpi.materials import MDPIMaterialsFetcher
from tests.collection.ingestion.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_MDPI_MOD = "app.collection.ingestion.fetchers.mdpi._common"
_FIXTURE_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "fixtures"
    / "mdpi_crossref.json"
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text())


def _items() -> list[dict[str, Any]]:
    return list(_load_fixture()["message"]["items"])


def _direct_outcomes() -> list[FetchOutcome]:
    fetcher = MDPIMaterialsFetcher()
    return [fetcher._convert_record(item, 1) for item in _items()]


def _mock_response(data: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        text=json.dumps(data),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://api.crossref.org/works"),
    )


def _mock_safe_client(response_or_exception: Any) -> MagicMock:
    client = AsyncMock()
    if isinstance(response_or_exception, BaseException):
        client.get = AsyncMock(side_effect=response_or_exception)
    else:
        client.get = AsyncMock(return_value=response_or_exception)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


async def _collect(it: AsyncIterator[FetchOutcome]) -> list[FetchOutcome]:
    return [o async for o in it]


# ---------------------------------------------------------------------------
# helpers (純関数)
# ---------------------------------------------------------------------------


class TestStripJats:
    def test_strips_jats_paragraph_tag(self) -> None:
        assert _strip_jats("<jats:p>hello world</jats:p>") == "hello world"

    def test_strips_nested_html(self) -> None:
        assert _strip_jats("<p>hi <em>there</em></p>") == "hi there"

    def test_normalizes_whitespace(self) -> None:
        assert _strip_jats("a   b\n\nc") == "a b c"

    def test_empty_string(self) -> None:
        assert _strip_jats("") == ""


class TestParseDateParts:
    def test_full_ymd(self) -> None:
        published = _parse_date_parts({"published": {"date-parts": [[2026, 5, 1]]}})
        assert published is not None
        assert published.value.isoformat().startswith("2026-05-01")

    def test_year_month_only(self) -> None:
        published = _parse_date_parts({"published": {"date-parts": [[2026, 5]]}})
        assert published is not None
        assert published.value.day == 1

    def test_year_only(self) -> None:
        published = _parse_date_parts({"published": {"date-parts": [[2026]]}})
        assert published is not None
        assert published.value.month == 1
        assert published.value.day == 1

    def test_falls_back_to_issued_when_published_missing(self) -> None:
        published = _parse_date_parts({"issued": {"date-parts": [[2026, 3, 15]]}})
        assert published is not None
        assert published.value.day == 15

    def test_returns_none_when_all_blocks_missing(self) -> None:
        assert _parse_date_parts({}) is None


class TestValidateLicense:
    def test_cc_by_4_url_passes(self) -> None:
        assert _validate_license(
            {"license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}]}
        )

    def test_cc_by_nc_fails(self) -> None:
        assert not _validate_license(
            {"license": [{"URL": "https://creativecommons.org/licenses/by-nc/4.0/"}]}
        )

    def test_missing_license_key_fails(self) -> None:
        assert not _validate_license({})


class TestExtractAuthors:
    def test_family_and_given_combined(self) -> None:
        authors = _extract_authors({"author": [{"family": "Smith", "given": "John"}]})
        assert authors == ["Smith John"]

    def test_family_only_when_given_missing(self) -> None:
        assert _extract_authors({"author": [{"family": "Mononym"}]}) == ["Mononym"]

    def test_empty_when_author_key_missing(self) -> None:
        assert _extract_authors({}) == []


class TestExtractDoi:
    def test_extracts_doi_string(self) -> None:
        assert _extract_doi({"DOI": "10.3390/ma17020001"}) == "10.3390/ma17020001"

    def test_returns_none_when_doi_missing(self) -> None:
        assert _extract_doi({}) is None


# ---------------------------------------------------------------------------
# _convert_record の Failed 分岐
# ---------------------------------------------------------------------------


class TestConvertRecordFailureBranches:
    def _base_valid_item(self) -> dict[str, Any]:
        return deepcopy(_items()[0])

    def test_non_journal_article_type_dropped(self) -> None:
        item = self._base_valid_item()
        item["type"] = "correction"
        outcome = MDPIMaterialsFetcher()._convert_record(item, 1)
        assert isinstance(outcome, Failed)
        assert outcome.reason.detail == "non_research_type"

    def test_non_cc_by_license_dropped(self) -> None:
        item = self._base_valid_item()
        item["license"] = [{"URL": "https://creativecommons.org/licenses/by-nc/4.0/"}]
        outcome = MDPIMaterialsFetcher()._convert_record(item, 1)
        assert isinstance(outcome, Failed)
        assert outcome.reason.detail == "non_cc_by"

    def test_missing_license_dropped(self) -> None:
        item = self._base_valid_item()
        del item["license"]
        outcome = MDPIMaterialsFetcher()._convert_record(item, 1)
        assert isinstance(outcome, Failed)
        assert outcome.reason.detail == "non_cc_by"

    def test_missing_title_returns_failed(self) -> None:
        item = self._base_valid_item()
        item["title"] = []
        outcome = MDPIMaterialsFetcher()._convert_record(item, 1)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "title_missing"

    def test_short_abstract_returns_body_too_short(self) -> None:
        item = self._base_valid_item()
        item["abstract"] = "<jats:p>too short</jats:p>"
        outcome = MDPIMaterialsFetcher()._convert_record(item, 1)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "body_too_short"

    def test_missing_date_parts_returns_published_at_missing(self) -> None:
        item = self._base_valid_item()
        for key in ("published", "issued", "published-online", "published-print"):
            item.pop(key, None)
        outcome = MDPIMaterialsFetcher()._convert_record(item, 1)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "published_at_missing"

    def test_missing_doi_returns_extraction_empty(self) -> None:
        item = self._base_valid_item()
        del item["DOI"]
        outcome = MDPIMaterialsFetcher()._convert_record(item, 1)
        assert isinstance(outcome, Failed)
        assert outcome.reason.code == "extraction_empty"
        assert outcome.reason.detail == "doi_missing"


# ---------------------------------------------------------------------------
# 不変条件 (passport / PROVIDES / audit-safe)
# ---------------------------------------------------------------------------


class TestPersistenceInvariants:
    def test_at_least_one_passport_yielded(self) -> None:
        assert_at_least_one_passport(_direct_outcomes())

    def test_passports_satisfy_persistence_invariants(self) -> None:
        assert_passports_persistable(_direct_outcomes())

    def test_provides_contract_holds(self) -> None:
        assert_provides_contract(_direct_outcomes(), MDPIMaterialsFetcher.PROVIDES)

    def test_metadata_audit_safe(self) -> None:
        assert_metadata_audit_safe(_direct_outcomes())

    def test_doi_url_used_as_source_url(self) -> None:
        outcomes = _direct_outcomes()
        passports = [
            o
            for o in outcomes
            if isinstance(o, FetchedEntry) and isinstance(o.item, ReadyForArticle)
        ]
        assert passports
        for entry in passports:
            assert isinstance(entry.item, ReadyForArticle)
            assert str(entry.item.source_url).startswith("https://doi.org/10.3390/")


# ---------------------------------------------------------------------------
# fetch pipeline (httpx mock 経路)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_yields_passports_for_valid_items() -> None:
    cm = _mock_safe_client(_mock_response(_load_fixture()))
    with patch(f"{_MDPI_MOD}.make_safe_async_client", return_value=cm):
        outcomes = await _collect(MDPIMaterialsFetcher().fetch(1))
    assert_at_least_one_passport(outcomes)
    passports = [
        o
        for o in outcomes
        if isinstance(o, FetchedEntry) and isinstance(o.item, ReadyForArticle)
    ]
    # fixture 3 records: valid / correction (drop) / no-license (drop) → 1 passport
    assert len(passports) == 1


@pytest.mark.asyncio
async def test_fetch_403_classified_as_permanent() -> None:
    response = _mock_response({}, status_code=403)
    error = httpx.HTTPStatusError("403", request=response.request, response=response)
    cm = _mock_safe_client(error)
    with (
        patch(f"{_MDPI_MOD}.make_safe_async_client", return_value=cm),
        pytest.raises(PermanentFetchError),
    ):
        await _collect(MDPIMaterialsFetcher().fetch(1))


@pytest.mark.asyncio
async def test_fetch_5xx_classified_as_temporary() -> None:
    response = _mock_response({}, status_code=500)
    error = httpx.HTTPStatusError("500", request=response.request, response=response)
    cm = _mock_safe_client(error)
    with (
        patch(f"{_MDPI_MOD}.make_safe_async_client", return_value=cm),
        pytest.raises(TemporaryFetchError),
    ):
        await _collect(MDPIMaterialsFetcher().fetch(1))


@pytest.mark.asyncio
async def test_request_uses_polite_pool_user_agent() -> None:
    cm = _mock_safe_client(_mock_response({"message": {"items": []}}))
    with patch(f"{_MDPI_MOD}.make_safe_async_client", return_value=cm) as mocked:
        await _collect(MDPIMaterialsFetcher().fetch(1))
    headers = mocked.call_args.kwargs["headers"]
    assert "mailto:" in headers["User-Agent"]


@pytest.mark.asyncio
async def test_request_filter_carries_issn_and_lookback() -> None:
    cm = _mock_safe_client(_mock_response({"message": {"items": []}}))
    with patch(f"{_MDPI_MOD}.make_safe_async_client", return_value=cm):
        await _collect(MDPIMaterialsFetcher().fetch(1))
    inner: AsyncMock = cm.__aenter__.return_value
    params = inner.get.call_args.kwargs["params"]
    flt = params["filter"]
    assert f"issn:{MDPIMaterialsFetcher.ISSN}" in flt
    assert "from-pub-date:" in flt
    assert params["sort"] == "published"
    assert params["order"] == "desc"
    assert params["rows"] == BaseMDPICrossrefFetcher.ROWS_PER_REQUEST
