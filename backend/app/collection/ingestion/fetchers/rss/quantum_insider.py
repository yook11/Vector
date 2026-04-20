"""The Quantum Insider RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher


class QuantumInsiderFetcher(BaseRssFetcher):
    """The Quantum Insider 用フェッチャー。デフォルトの convert_entry を継承。"""
