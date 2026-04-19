"""Krebs on Security RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class KrebsOnSecurityFetcher(BaseRssFetcher):
    """Krebs on Security 用フェッチャー。デフォルトの convert_entry を継承。"""
