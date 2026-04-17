"""Yahoo Finance RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class YahooFinanceFetcher(BaseRssFetcher):
    """Yahoo Finance 用フェッチャー。デフォルトの convert_entry を継承。"""
