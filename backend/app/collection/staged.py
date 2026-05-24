"""ingestion task 群の kiq 引数 envelope。

taskiq は kiq 引数に Pydantic ``BaseModel(frozen=True)`` を要求する
(素の ``dataclass`` は serializer が ``PydanticSerializationError`` で死ぬ)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AcquireSourceArg(BaseModel):
    """``acquire_source`` task の kiq 引数 envelope。

    ``id``: ``news_sources.id`` (Article の FK で使う)。
    ``name``: ``news_sources.name`` (StrEnum 値、Fetcher dispatch の lookup キー)。
    ``dispatch_sources`` で 1 度 ``NewsSource`` を query して組み立て、以降の
    ``NewsSource`` 再 lookup を不要にする。
    """

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
