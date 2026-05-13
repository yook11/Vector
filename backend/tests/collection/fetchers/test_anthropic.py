"""``AnthropicFetcher`` (sitemap-only Pattern H) の不変条件テスト。

sitemap.xml から ``<loc>`` + ``<lastmod>`` を抽出し ``/news/`` で始まる URL
だけを yield する Fetcher。BaseSitemapFetcher の XXE 防御込みの fixture-based
テスト。
"""

from __future__ import annotations

from pathlib import Path

from app.collection.fetchers._base.sitemap import BaseSitemapFetcher
from app.collection.fetchers.anthropic import AnthropicFetcher
from app.collection.ingestion.domain.fetched_article import FetchOutcome
from tests.collection.fetchers._invariant import (
    assert_at_least_one_passport,
    assert_metadata_audit_safe,
    assert_passports_persistable,
    assert_provides_contract,
)

_FIXTURE = (
    Path(__file__).parent.parent.parent / "fixtures" / "anthropic_sitemap.xml"
)


def _outcomes() -> list[FetchOutcome]:
    fetcher = AnthropicFetcher()
    parsed = BaseSitemapFetcher._parse_sitemap(_FIXTURE.read_bytes())
    return [
        fetcher._convert_entry(loc, lastmod, 1)
        for loc, lastmod in parsed
        if fetcher._url_matches(loc)
    ]


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_outcomes())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_outcomes())


def test_provides_contract_holds() -> None:
    assert_provides_contract(_outcomes(), AnthropicFetcher.PROVIDES)


def test_metadata_audit_safe() -> None:
    assert_metadata_audit_safe(_outcomes())


def test_xxe_external_entity_disabled() -> None:
    """sitemap parser は外部実体参照を解決しない (defensive parsing 契約)。"""
    malicious = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.anthropic.com/news/&xxe;</loc></url>
</urlset>
"""
    entries = BaseSitemapFetcher._parse_sitemap(malicious)
    loc = entries[0][0] if entries else ""
    assert "/etc/passwd" not in loc
    assert "root:" not in loc
