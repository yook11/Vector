"""EE Times Japan RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class EETimesJapanFetcher(BaseRssFetcher):
    """EE Times Japan 用フェッチャー。デフォルトの convert_entry を継承。"""
