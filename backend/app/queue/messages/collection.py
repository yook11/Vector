"""collection (acquisition) の kiq message DTO。

taskiq の formatter は素の ``dataclass`` を扱えず ``PydanticSerializationError``
で死ぬため (Issue #441 / #558)、kiq 引数は必ず Pydantic ``BaseModel(frozen=True)``
を採る (`feedback_taskiq_basemodel_required.md`)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AcquireSourceTaskInput(BaseModel):
    """``acquire_source`` task の kiq 引数 envelope。

    ``id``: ``news_sources.id`` (Article の FK で使う)。
    ``name``: ``news_sources.name`` (StrEnum 値、Fetcher dispatch の lookup キー)。
    ``dispatch_*`` が 1 度 ``NewsSource`` を query して組み立て、以降の
    ``NewsSource`` 再 lookup を不要にする。
    """

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
