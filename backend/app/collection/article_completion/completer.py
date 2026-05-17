"""Stage 2 の補完境界 — ``ObservedArticle`` を完成形に解決する純粋関数。

``ArticleCompletionService`` が「資格判定 → 完成 → 分類 → 後始末 → 永続化」を
1 メソッドに混ぜていた問題を解くため、**完成させる**責務だけをここに切り出す。

本境界の契約:

- 出力は ``AnalyzableArticle | CompletionFailure`` の**閉じた値 union**で型保証。
  成功 / 名前付き失敗のどちらかしか返らない。
- **副作用なし** — DB / log / ``failure_handler`` を一切呼ばない。
- fetch の ``ExternalFetchError`` (例外) は境界で ``FetchFailed`` (値) に畳む。
  これで失敗 3 種 (fetch 例外 / ``ExtractionEmpty`` / ``ArticleCompletionFailed``)
  が単一の閉じ union に揃い、caller は ``isinstance`` 1 回 + 委譲で読める。

merge は ``complete_with_html`` (profile 駆動の純粋関数) に委譲する。HTML 抽出
結果が ``ExtractionEmpty`` でも値のまま渡し、``body=html_required`` のとき
``complete_with_html`` 内で ``ExtractionEmpty`` を値返しする (旧 completer の
短絡と等価。spec §7 等価表)。本境界は profile を知らず Ready 経由で受け取る。

失敗を ``CompletionDisposition`` に分類するのは ``disposition.py``、状態遷移 + log
は ``failure_handling.py`` の責務。本境界はそのどちらも知らない (責務をファイルで
分離)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.collection.article_completion.extractor import (
    ArticleHtmlExtractor,
    ExtractionEmpty,
)
from app.collection.article_completion.promotion import complete_with_html
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.completion import ArticleCompletionFailed
from app.collection.external_fetch_errors import ExternalFetchError


@dataclass(frozen=True, slots=True)
class FetchFailed:
    """origin fetch が ``ExternalFetchError`` で失敗したことを表す値。

    境界で例外を値に畳むためのラッパ。元の例外は ``error`` に保持し、分類
    (``classify_external_fetch_error``) と log の ``error_class`` で使う。
    """

    error: ExternalFetchError


CompletionFailure = FetchFailed | ExtractionEmpty | ArticleCompletionFailed
"""補完が失敗する 3 形を 1 つに揃えた閉じた値 union。

- ``FetchFailed``: origin fetch 例外を畳んだ値。
- ``ExtractionEmpty``: 取れたが使える本文でない (extractor の値)。
- ``ArticleCompletionFailed``: merge / invariant 違反 (domain の値)。
"""


class ArticleHtmlCompleter:
    """``ObservedArticle`` を HTML 取得 + profile 駆動 merge で完成させる純粋境界。

    ``complete`` が単一エントリポイント。副作用を持たず、出力型を閉じた union で
    保証することだけが責務。``extractor_factory`` は test seam (default は
    live extractor)。
    """

    def __init__(
        self,
        extractor_factory: Callable[[], ArticleHtmlExtractor] = ArticleHtmlExtractor,
    ) -> None:
        self._extractor_factory = extractor_factory

    async def complete(
        self, ready: ReadyForArticleCompletion
    ) -> AnalyzableArticle | CompletionFailure:
        """HTML 取得 → profile 駆動 merge で ``AnalyzableArticle`` を解決する。

        fetch origin failure は ``FetchFailed`` に畳む。``ExtractionEmpty`` /
        promotion 失敗 (``ArticleCompletionFailed``) は ``complete_with_html``
        が値で返す。成功時のみ昇格済 ``AnalyzableArticle`` を返す。例外は外に
        出さない (境界で値に畳む)。
        """
        extractor = self._extractor_factory()

        try:
            html_result = await extractor.fetch(ready.source_url.as_safe_url())
        except ExternalFetchError as exc:
            return FetchFailed(error=exc)

        return complete_with_html(
            ready.observed,
            ready.profile,
            html_result,
            source_id=ready.source_id,
            source_url=ready.source_url,
        )
