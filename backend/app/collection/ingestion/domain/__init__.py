"""ingestion BC のドメイン層。

外部ソース (RSS / HN API 等) から発見された記事候補の概念を表現する:

- ``ArticleCandidate`` — fetcher 境界 (技術境界) の正規化 VO。
  外部生文字列 → URL 安全性 / タイトル整形済みの中間表現。
"""

from app.collection.ingestion.domain.value_objects import ArticleCandidate

__all__ = [
    "ArticleCandidate",
]
