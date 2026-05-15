"""SourceAdapter が yield する中間型 + Adapter Protocol。

per-source の raw 取得結果を共通言語に翻訳する責務 (External boundary →
Internal validation の境界層) を表現する。Adapter 自身は ``ReadyForArticle``
/ ``IncompleteArticle`` を構築しない: 品質ゲート判定は ``passport_builder``
に委ねる。

``FetchedArticle`` field 設計の意図:

- ``title`` / ``url`` は ``str`` (``Optional`` ではない)。"取れなかった" は
  空 str で表現し、品質ゲートで一律 drop する (caller が ``None`` か空白かを
  迷う API を増やさない)。
- ``body`` / ``published_at`` は ``Optional``。``None`` は「不在」の意味付きで、
  Stage 2 HTML 補完に委ねるシグナル。
- ``prefer_html_title`` は「現 title は仮タイトル」を表す flag。``True`` のとき
  Ready 経路を止め (HTML 補完で title 上書きの機会を残す)、``IncompleteArticle``
  経路に固定する。sitemap / HTML listing 系で必須。
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
    本データから passport (``ReadyForArticle`` | ``IncompleteArticle``) を
    組むのは ``passport_builder`` の責務。
    """

    title: str
    url: str
    body: str | None
    published_at: datetime | None
    prefer_html_title: bool = False


class SourceAdapter(Protocol):
    """外部 source ごとの「raw 取得 + 共通言語化」責務。

    filter / dedup / source 固有の取得 logic は Adapter 内部で完結させ、
    外には "次工程に渡せる" ``FetchedArticle`` だけを yield する
    (Outcome 純化原則)。

    ``NAME`` / ``ENDPOINT_URL`` は ``Fetcher`` Protocol との互換のため宣言する
    が、実装は ``ClassVar[str]`` でも instance attr でも満たせるよう ``str`` で
    緩く受ける。``isinstance`` チェックは行わない (composition root での静的
    配線が前提) ため ``@runtime_checkable`` は付けない。
    """

    NAME: str
    ENDPOINT_URL: str

    def collect(self) -> AsyncIterator[FetchedArticle]: ...
