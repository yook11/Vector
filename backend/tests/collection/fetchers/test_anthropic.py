"""``AnthropicFetcher`` (sitemap-only Pattern H) の不変条件テスト。

sitemap.xml から ``<loc>`` + ``<lastmod>`` を抽出し ``/news/`` で始まる URL
だけを yield する Fetcher。BaseSitemapFetcher の XXE 防御込みの fixture-based
テスト。
"""

from __future__ import annotations

from pathlib import Path

from app.collection.fetchers._base.sitemap import BaseSitemapFetcher
from app.collection.fetchers.anthropic import AnthropicFetcher
from tests.collection.fetchers._invariant import (
    Passport,
    assert_at_least_one_passport,
    assert_passports_persistable,
)

_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "anthropic_sitemap.xml"


def _passports() -> list[Passport]:
    fetcher = AnthropicFetcher()
    parsed = BaseSitemapFetcher._parse_sitemap(_FIXTURE.read_bytes())
    items: list[Passport] = []
    for loc, lastmod in parsed:
        if not fetcher._url_matches(loc):
            continue
        converted = fetcher._convert_entry(loc, lastmod, 1)
        if converted is not None:
            items.append(converted)
    return items


def test_fixture_yields_at_least_one_passport() -> None:
    assert_at_least_one_passport(_passports())


def test_passports_satisfy_persistence_invariants() -> None:
    assert_passports_persistable(_passports())


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
