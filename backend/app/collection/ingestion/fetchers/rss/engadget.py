"""Engadget RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class EngadgetFetcher(BaseRssFetcher):
    """Engadget 用フェッチャー。デフォルトの convert_entry を継承。"""
