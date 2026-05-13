"""Fetcher 出口型 — 各 Fetcher が ``AsyncIterator[FetchOutcome]`` で yield する型。

``ReadyForArticle`` (Pattern R) / ``IncompleteArticle`` (Pattern H) が次工程
進行保証型で、何が取れようがこれを満たして次工程に渡す。``metadata`` は
``FetchedEntry`` で Service まで運ばれ、Stage 1 で ``pipeline_events.payload``
に焼き付けた後は以降の段階に運ばない。

PR 3 で BC 境界を整理: 旧 ``ingestion/domain/fetched_article.py`` から
``SourceFetchFailed`` 系 + envelope 型を本ファイルに移管。``IncompleteArticle`` /
``ReadyForArticle`` は ``incomplete_article`` / ``article`` aggregate に移管。

依存方向: ``fetchers/outcome.py`` は aggregate を import する (envelope 型として
parametrize)。**aggregate 側は絶対に本ファイルを import しない** (循環防止 +
BC 境界の依存方向維持)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.collection.article.domain.article import ReadyForArticle
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

SourceFetchFailureCode = Literal[
    "http_transient",
    "http_blocked",
    "paywalled",
    "extraction_empty",
    "body_too_short",
    "title_missing",
    "published_at_missing",
    "link_target_failed",
    "other",
]
"""Fetcher が yield する失敗種別 (取得 / 抽出フェーズの失敗)。

``ArticleCompletionFailureCode`` (HTML 補完失敗) とは型として完全分離だが、
文字列値は audit key の前方互換のため重複部 (``published_at_missing`` /
``other``) を共有する。
"""


class SourceFetchFailureReason(BaseModel):
    """取得失敗の理由。

    ``retryable`` は scheduler 再投入判定。``detail`` は同 ``code`` の細分化。
    """

    model_config = ConfigDict(frozen=True)

    code: SourceFetchFailureCode
    retryable: bool
    detail: str | None = None


class SourceFetchFailed(BaseModel):
    """Fetcher が yield する取得失敗。Service が ``failed_codes`` に集計する。"""

    model_config = ConfigDict(frozen=True)

    reason: SourceFetchFailureReason


@dataclass(frozen=True, slots=True)
class FetchedEntry:
    """Fetcher が 1 entry 分 yield する単位 (Service 内消費のみ、kiq に乗らない)。

    ``metadata`` は opaque な dict。Fetcher 側で primitive (str/int/list/dict)
    に正規化してから格納する責務 (URL は ``str(url)`` 化、datetime は
    ``isoformat()`` 化)。``pipeline_events.payload`` (JSONB) に直接焼かれる。
    """

    item: ReadyForArticle | IncompleteArticle
    metadata: Mapping[str, Any]


FetchOutcome = FetchedEntry | SourceFetchFailed
