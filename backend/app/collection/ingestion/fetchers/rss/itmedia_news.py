"""ITmedia NEWS RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class ITmediaNewsFetcher(BaseRssFetcher):
    """ITmedia NEWS 用フェッチャー。デフォルトの convert_entry を継承。"""
