"""Collection 層の共通ドメイン例外。

ingestion / extraction の両方で、外部リソース取得の失敗を
リトライ可否の観点で分類する例外。
"""

from __future__ import annotations


class PermanentFetchError(Exception):
    """リトライ不可のフェッチ失敗（403 / 404 / robots.txt で拒否）。"""


class TemporaryFetchError(Exception):
    """リトライ可能なフェッチ失敗（5xx / タイムアウト / 429）。"""
