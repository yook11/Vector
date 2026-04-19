"""IEEE Spectrum RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class IEEESpectrumFetcher(BaseRssFetcher):
    """IEEE Spectrum 用フェッチャー。デフォルトの convert_entry を継承。"""
