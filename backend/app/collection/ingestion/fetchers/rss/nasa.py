"""NASA RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class NASAFetcher(BaseRssFetcher):
    """NASA 用フェッチャー。デフォルトの convert_entry を継承。"""
