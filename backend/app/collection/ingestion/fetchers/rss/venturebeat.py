"""VentureBeat RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class VentureBeatFetcher(BaseRssFetcher):
    """VentureBeat 用フェッチャー。デフォルトの convert_entry を継承。"""
