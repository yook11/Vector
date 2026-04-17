"""TechCrunch RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class TechCrunchFetcher(BaseRssFetcher):
    """TechCrunch 用フェッチャー。デフォルトの convert_entry を継承。"""
