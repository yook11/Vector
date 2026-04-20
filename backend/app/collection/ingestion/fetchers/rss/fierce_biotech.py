"""FierceBiotech RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class FierceBiotechFetcher(BaseRssFetcher):
    """FierceBiotech 用フェッチャー。デフォルトの convert_entry を継承。"""
