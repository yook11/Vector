from pydantic import Field

from app.schemas.base import _CamelBase

# to_camel は ``_24h`` を ``24H`` に変換するため、
# API 契約の小文字 ``24h`` を alias で明示する。
_ARTICLE_COUNT_24H_ALIAS = "articleCount24h"


class Category(_CamelBase):
    """カテゴリの参照情報（表示・絞り込み用）。

    name は表示用、slug は絞り込みキー。id は持たない（表示と絞り込みに不要）。
    """

    slug: str
    name: str


class CategoryStats(_CamelBase):
    """カテゴリについての集計値。

    件数は記事の公開日時ではなく、分析が終わった時刻 (analyzed_at) で数える。
    """

    category: Category
    article_count_24h: int = Field(
        validation_alias=_ARTICLE_COUNT_24H_ALIAS,
        serialization_alias=_ARTICLE_COUNT_24H_ALIAS,
    )


class CategoryStatsList(_CamelBase):
    """GET /api/v1/categories のレスポンス。"""

    items: list[CategoryStats]
