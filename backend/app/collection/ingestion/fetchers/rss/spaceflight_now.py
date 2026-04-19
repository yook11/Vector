"""Spaceflight Now RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class SpaceflightNowFetcher(BaseRssFetcher):
    """Spaceflight Now 用フェッチャー。デフォルトの convert_entry を継承。"""
