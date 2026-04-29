"""Electrek RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class ElectrekFetcher(BaseRssFetcher):
    """Electrek 用フェッチャー。デフォルトの convert_entry を継承。"""
