"""SourceAdapter が yield する中間型 + 取得 machinery Protocol。

per-source の raw 取得結果を共通言語に翻訳する責務 (External boundary →
Internal validation の境界層) を表現する。Adapter 自身は ``AnalyzableArticle``
/ ``ObservedArticle`` を構築しない: 品質ゲート判定は ``passport_builder``
に委ねる。

``FetchedArticle`` field 設計の意図:

- ``title`` / ``url`` は ``str`` (``Optional`` ではない)。"取れなかった" は
  空 str で表現し、品質ゲートで一律 drop する (caller が ``None`` か空白かを
  迷う API を増やさない)。
- ``body`` / ``published_at`` は ``Optional``。``None`` は「不在」の意味付きで、
  Stage 2 HTML 補完に委ねるシグナル。

``SourceAdapter`` は「どう取るか」だけの取得 machinery 契約 (P2)。ソースの
identity (``name`` / ``endpoint_url``) と補完方針 (``observed_origin`` /
``completion_profile``) は machinery の関心ではなく ``ArticleSource`` 集約
(``sources/article_source.py``) が所有する。Adapter は ``ArticleSource`` の
``adapter_factory`` から構築され、``collect()`` だけを公開する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FetchedArticle:
    """1 entry / 1 record 分の取得材料。

    Adapter が外部 source から取り出した raw データを共通言語に揃えた中間型。
    本データから passport (``AnalyzableArticle`` | ``ObservedArticle``) を
    組むのは ``passport_builder`` の責務。
    """

    title: str
    url: str
    body: str | None
    published_at: datetime | None


class SourceAdapter(Protocol):
    """外部 source ごとの「raw 取得 + 共通言語化」machinery 契約。

    filter / dedup / source 固有の取得 logic は Adapter 内部で完結させ、
    外には "次工程に渡せる" ``FetchedArticle`` だけを yield する
    (Outcome 純化原則)。``isinstance`` チェックは行わない (composition root
    での静的配線が前提) ため ``@runtime_checkable`` は付けない。

    本 Protocol は「どう取るか」のみを表す (P2)。per-source の identity /
    補完方針は ``ArticleSource`` 集約が所有し、Adapter は ``adapter_factory``
    から必要な config を ``__init__`` で受け取って構築される。よって
    ``NAME`` / ``observed_origin`` / ``completion_profile`` は本契約に含めない。
    """

    def collect(self) -> AsyncIterator[FetchedArticle]: ...
