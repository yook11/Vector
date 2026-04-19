"""JPCERT/CC RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class JPCERTFetcher(BaseRssFetcher):
    """JPCERT/CC 用フェッチャー。デフォルトの convert_entry を継承。"""
