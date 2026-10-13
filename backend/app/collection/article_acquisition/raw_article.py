"""``XxxSource.collect`` が yield する中間型。

per-source の raw 取得結果を共通言語に揃える境界 (External → Internal)。
Source 自身は獲得型を構築せず、受け付けの判定は ``FetchedArticle.from_raw`` に
委ねる。``title`` / ``url`` は空 str で不在を表し、``body`` / ``published_at`` の
``None`` は不在シグナル。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RawArticle:
    """1 entry / 1 record 分の取得材料 (raw を共通言語に揃えた中間型)。"""

    title: str
    url: str
    body: str | None
    published_at: datetime | None
