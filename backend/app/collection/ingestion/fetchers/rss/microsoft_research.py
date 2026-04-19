"""Microsoft Research RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class MicrosoftResearchFetcher(BaseRssFetcher):
    """Microsoft Research 用フェッチャー。デフォルトの convert_entry を継承。"""
