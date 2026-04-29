"""MONOist RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class MONOistFetcher(BaseRssFetcher):
    """MONOist 用フェッチャー。デフォルトの convert_entry を継承。"""
