"""ITmediaFetcher の convert_entry テスト。"""

from app.collection.ingestion.fetchers.rss.itmedia import ITmediaFetcher


class TestITmediaConvertEntry:
    def test_strips_ascii_section_prefix(self) -> None:
        """[ITmedia News] のような ASCII セクション接頭辞を除去する。"""
        entry = {
            "link": "https://itmedia.co.jp/article-1",
            "title": "[ITmedia News] Test Article",
            "summary": "Summary",
        }
        fetcher = ITmediaFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.title == "Test Article"

    def test_strips_prefix_with_spaces(self) -> None:
        """[ITmedia PC USER] のように空白を含むセクション名を除去する。"""
        entry = {
            "link": "https://itmedia.co.jp/article-2",
            "title": "[ITmedia PC USER] Hardware Review",
            "summary": "Summary",
        }
        fetcher = ITmediaFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.title == "Hardware Review"

    def test_strips_multibyte_prefix(self) -> None:
        """マルチバイト文字を含む接頭辞を除去する。"""
        entry = {
            "link": "https://itmedia.co.jp/article-3",
            "title": "[ITmedia エンタープライズ] Enterprise News",
            "summary": "Summary",
        }
        fetcher = ITmediaFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.title == "Enterprise News"

    def test_preserves_title_without_prefix(self) -> None:
        """接頭辞がないタイトルはそのまま保持する。"""
        entry = {
            "link": "https://itmedia.co.jp/article-4",
            "title": "No Prefix Article",
            "summary": "Summary",
        }
        fetcher = ITmediaFetcher()
        candidate = fetcher.convert_entry(entry)

        assert candidate is not None
        assert candidate.title == "No Prefix Article"

    def test_returns_none_for_empty_url(self) -> None:
        entry = {"link": "", "title": "[ITmedia News] No URL", "summary": "Summary"}
        fetcher = ITmediaFetcher()
        assert fetcher.convert_entry(entry) is None
