"""``app.collection.url_canonicalize.canonicalize_url`` の不変条件テスト。

spec PR2.5-A の 5 項目それぞれと、idempotence / 表面で combined 振る舞いを検証する。
"""

from __future__ import annotations

import pytest

from app.collection.url_canonicalize import canonicalize_url


class TestCanonicalizeUrl:
    """spec 5 項目の不変条件と組合せ挙動。"""

    def test_lowercase_host(self) -> None:
        # host は case-insensitive (RFC 3986) なので大文字を消し dup を防ぐ
        assert (
            canonicalize_url("https://EXAMPLE.com/article")
            == "https://example.com/article"
        )

    def test_lowercase_host_preserves_path_case(self) -> None:
        # path は case-sensitive、host のみを小文字化する
        assert (
            canonicalize_url("https://Example.COM/Article/Path")
            == "https://example.com/Article/Path"
        )

    def test_strip_tracking_utm(self) -> None:
        # utm_* は除去対象
        assert (
            canonicalize_url(
                "https://example.com/a?utm_source=foo&utm_medium=email&id=42"
            )
            == "https://example.com/a?id=42"
        )

    def test_strip_tracking_click_ids(self) -> None:
        # 広告 click ID は除去対象
        url = "https://example.com/a?fbclid=AB&gclid=CD&dclid=EF&msclkid=GH&id=1"
        assert canonicalize_url(url) == "https://example.com/a?id=1"

    def test_strip_tracking_mailchimp_and_ref(self) -> None:
        url = "https://example.com/a?mc_cid=AA&mc_eid=BB&ref=cc&ref_src=dd&referrer=ee&q=v"
        assert canonicalize_url(url) == "https://example.com/a?q=v"

    def test_keep_non_tracking_query(self) -> None:
        # 記事 ID 等の必須 query は順序保ったまま保持
        assert (
            canonicalize_url("https://example.com/a?id=42&page=2")
            == "https://example.com/a?id=42&page=2"
        )

    def test_trailing_slash_stripped(self) -> None:
        # path 末尾の / は除去 (dup 回避)
        assert (
            canonicalize_url("https://example.com/article/")
            == "https://example.com/article"
        )

    def test_root_path_slash_kept(self) -> None:
        # root path の / は保持 (除去すると path 空で意味が変わる)
        assert canonicalize_url("https://example.com/") == "https://example.com/"

    def test_multi_trailing_slash_stripped(self) -> None:
        assert canonicalize_url("https://example.com/a///") == "https://example.com/a"

    def test_fragment_removed(self) -> None:
        # fragment は browser-only の概念、URL identity には含めない
        assert (
            canonicalize_url("https://example.com/a#section-2")
            == "https://example.com/a"
        )

    def test_scheme_preserved_http(self) -> None:
        # http と https は別 URL として扱う (リダイレクト判定は別軸)
        assert canonicalize_url("http://example.com/a") == "http://example.com/a"
        assert canonicalize_url("https://example.com/a") == "https://example.com/a"

    def test_combined_transformation(self) -> None:
        # 全 5 項目が同時に適用される
        raw = "HTTPS://Example.COM/Article/?utm_source=x&id=42#section"
        assert canonicalize_url(raw) == "https://example.com/Article?id=42"

    def test_idempotent(self) -> None:
        # 冪等性: canonicalize 済 URL を再投入しても同じ結果
        urls = [
            "https://example.com/article",
            "https://example.com/",
            "http://example.com/path?id=1",
            "HTTPS://EXAMPLE.com/A/?utm_source=x#frag",
        ]
        for raw in urls:
            once = canonicalize_url(raw)
            twice = canonicalize_url(once)
            assert once == twice, f"idempotence violated for {raw!r}"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # 全 tracking のみの query → query 部丸ごと消える
            (
                "https://example.com/a?utm_source=x&fbclid=y",
                "https://example.com/a",
            ),
            # 末尾 / + fragment + tracking
            (
                "https://example.com/a/?utm_source=x#top",
                "https://example.com/a",
            ),
            # query without value (keep_blank_values 想定の挙動を固定)
            (
                "https://example.com/a?id=&q=v",
                "https://example.com/a?id=&q=v",
            ),
        ],
    )
    def test_edge_cases(self, raw: str, expected: str) -> None:
        assert canonicalize_url(raw) == expected
