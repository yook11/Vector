"""``XxxSource.collect`` が yield する中間型 (P2-D)。

per-source の raw 取得結果を共通言語に翻訳する責務 (External boundary →
Internal validation の境界層) を表現する。Source 自身は ``AnalyzableArticle``
/ ``ObservedArticle`` を構築しない: 品質ゲート判定は ``passport_builder``
に委ねる。

``FetchedArticle`` field 設計の意図:

- ``title`` / ``url`` は ``str`` (``Optional`` ではない)。"取れなかった" は
  空 str で表現し、品質ゲートで一律 drop する (caller が ``None`` か空白かを
  迷う API を増やさない)。
- ``body`` / ``published_at`` は ``Optional``。``None`` は「不在」の意味付きで、
  Stage 2 HTML 補完に委ねるシグナル。

P2(B+C) までは取得 machinery 契約 ``SourceAdapter`` Protocol を本モジュールに
同居させていたが、P2-D で Adapter 概念を除去し ``ArticleSource`` Protocol
(``sources/article_source.py``) に一本化したため削除した。本モジュールは
中間値型 ``FetchedArticle`` のみを持つ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FetchedArticle:
    """1 entry / 1 record 分の取得材料。

    Source が外部 source から取り出した raw データを共通言語に揃えた中間型。
    本データから passport (``AnalyzableArticle`` | ``ObservedArticle``) を
    組むのは ``passport_builder`` の責務。
    """

    title: str
    url: str
    body: str | None
    published_at: datetime | None
