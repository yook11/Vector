import math
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

MAX_PAGE = 10_000
MAX_PER_PAGE = 100


class _CamelBase(BaseModel):
    """camelCase エイリアス生成を備えた全体共通スキーマ基底。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class PaginationParams(BaseModel):
    """共通のページネーションパラメータ。

    ルーターシグネチャでは ``Annotated[PaginationParams, Query()]``
    （またはそのサブクラス）として受け取る。``Depends()`` は使わないこと。
    ``Depends()`` はサブクラスの非スカラフィールド（VO 型など）を
    警告なしに黙って落とすため。feedback_vo_boundary memory を参照。
    """

    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1
    per_page: Annotated[int, Query(ge=1, le=MAX_PER_PAGE, alias="perPage")] = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def limit(self) -> int:
        return self.per_page

    def total_pages(self, total: int) -> int:
        return math.ceil(total / self.per_page) if total > 0 else 0
