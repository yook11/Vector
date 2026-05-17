"""SourceAdapter が yield する中間型 + Adapter Protocol。

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

「現 title は仮タイトルか」(sitemap / HTML listing 系で必須だった旧 HTML
優先 flag) は本中間型から除去済。仮タイトル性は per-source 知識のため
``SourceAdapter.completion_profile`` (title=``html_preferred``) が表現し、
``passport_builder`` が profile から Ready gate を決める (spec §3.3)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import SourceCompletionProfile


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
    """外部 source ごとの「raw 取得 + 共通言語化」責務。

    filter / dedup / source 固有の取得 logic は Adapter 内部で完結させ、
    外には "次工程に渡せる" ``FetchedArticle`` だけを yield する
    (Outcome 純化原則)。

    ``NAME`` / ``ENDPOINT_URL`` は ``Fetcher`` Protocol との互換のため宣言する
    が、実装は ``ClassVar[str]`` でも instance attr でも満たせるよう ``str`` で
    緩く受ける。``isinstance`` チェックは行わない (composition root での静的
    配線が前提) ため ``@runtime_checkable`` は付けない。

    ``observed_origin`` / ``completion_profile`` は per-source 知識
    (取得出自 / 補完方針)。共有基底 4 個が default
    (``feed`` / ``DEFAULT_PROFILE``) を持ち、特例 source のみ override する
    (spec §4.1)。``NAME`` 同様 ``ClassVar`` でも instance attr でも満たせる。
    """

    NAME: str
    ENDPOINT_URL: str
    observed_origin: ObservedOrigin
    completion_profile: SourceCompletionProfile

    def collect(self) -> AsyncIterator[FetchedArticle]: ...
