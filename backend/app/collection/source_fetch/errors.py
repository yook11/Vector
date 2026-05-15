"""Stage 1 (source_fetch) の Layer 1 marker。

``ingest_source`` task 層の唯一の dispatch 軸。``ArticleAcquisitionService`` の
boundary で origin ``ExternalFetchError`` を本 marker に wrap する。Stage 1 は
taskiq inline retry を持たない (cron 一本化、``max_retries=0``) ため marker は
1 種のみ — Stage 2 の ``Permanent`` / ``Temporary*`` のような細分は持たない
(原則: Stage 共通 marker は作らない、Stage 4 と同思想)。

``app.collection.errors.SourceFetchError`` (Stage 2 が継承軸で使用) と同名だが
別 module の別クラス。本 marker は Stage 1 runtime のみが触れ、Stage 2 とは
runtime で衝突しない。
"""

from __future__ import annotations


class SourceFetchError(Exception):
    """ソース全体の取得に失敗したことを示す Stage 1 marker。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            boundary で origin ``ExternalFetchError.CODE`` を引き継ぐ。
    """

    code: str

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
