"""TheRegisterFetcher の convert_entry テスト。"""

from app.collection.ingestion.fetchers.rss.the_register import TheRegisterFetcher


class TestTheRegisterConvertEntry:
    def test_normalizes_redirector_url(self) -> None:
        """go.theregister.com/feed/ プレフィックスを実 URL に正規化する。"""
        entry = {
            "link": "https://go.theregister.com/feed/www.theregister.com/2026/04/28/example_slug/",
            "title": "Example Article",
        }
        fetcher = TheRegisterFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert (
            str(candidate.url) == "https://www.theregister.com/2026/04/28/example_slug/"
        )

    def test_preserves_direct_url(self) -> None:
        """リダイレクタを通らない直接リンクはそのまま保持する。"""
        entry = {
            "link": "https://www.theregister.com/2026/04/28/direct_article/",
            "title": "Direct Article",
        }
        fetcher = TheRegisterFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert (
            str(candidate.url)
            == "https://www.theregister.com/2026/04/28/direct_article/"
        )

    def test_returns_none_for_empty_link(self) -> None:
        """link が空文字列で extract_guid も返さない場合は None。"""
        entry = {"link": "", "title": "No URL"}
        fetcher = TheRegisterFetcher()
        assert fetcher.convert_entry(entry) is None

    def test_falls_back_to_guid_when_link_missing(self) -> None:
        """link が空でも entry.id があればそれを利用する (extract_guid 経由)。"""
        entry = {
            "link": "",
            "id": "https://www.theregister.com/2026/04/28/from_guid/",
            "title": "Article from GUID",
        }
        fetcher = TheRegisterFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert str(candidate.url) == "https://www.theregister.com/2026/04/28/from_guid/"
