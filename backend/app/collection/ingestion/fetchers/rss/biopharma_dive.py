"""BioPharma Dive RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class BioPharmaFetcher(BaseRssFetcher):
    """BioPharma Dive 用フェッチャー。デフォルトの convert_entry を継承。"""
