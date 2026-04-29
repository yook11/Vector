"""SpaceNews RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class SpaceNewsFetcher(BaseRssFetcher):
    """SpaceNews 用フェッチャー。デフォルトの convert_entry を継承。"""
