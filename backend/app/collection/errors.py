"""Collection 層の共通ドメイン例外。

ingestion / extraction の両方で、外部リソース取得の失敗を
リトライ可否の観点で分類する例外。
"""

from __future__ import annotations


class PermanentFetchError(Exception):
    """リトライ不可のフェッチ失敗（403 / 404 / robots.txt で拒否）。"""


class TemporaryFetchError(Exception):
    """リトライ可能なフェッチ失敗（5xx / タイムアウト / 429）。"""


class DiscoveredArticleMissing(Exception):
    """キューで指定された DiscoveredArticle 行が DB に存在しない異常系。

    enqueue 後の手動削除・DB リセット vs キュー残留・環境取り違え等で発生し、
    正常運用では起きない。リトライ不能なので呼び出し側（Task）でメッセージを
    捨てる判断に使う。
    """

    def __init__(self, discovered_article_id: int) -> None:
        super().__init__(f"DiscoveredArticle(id={discovered_article_id}) not found")
        self.discovered_article_id = discovered_article_id
