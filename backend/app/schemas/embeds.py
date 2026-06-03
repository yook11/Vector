"""他の API レスポンスに埋め込まれる軽量スキーマ群。

これらのクラスはトップレベルの API レスポンスにはならない。
常に親レスポンススキーマ（NewsBrief, CategoryDetail など）内に
ネストされて利用される。
"""

from app.collection.sources.source_name import SourceName
from app.models.value_objects.category import CategoryName, CategorySlug
from app.schemas.base import _CamelBase
from app.shared.security.safe_url import SafeUrl


class NewsSourceEmbed(_CamelBase):
    """ニュースソースの基本参照情報（フィルタ・表示用）"""

    name: SourceName
    attribution_label: str | None = None


class CategoryEmbed(_CamelBase):
    """記事に紐づくカテゴリの参照情報（カード表示・絞り込み用）。

    name は表示用、slug は絞り込みキー。id は持たない（表示と絞り込みに不要）。
    サイドバー用の集計付き CategoryDetail とは役割が異なる。
    """

    slug: CategorySlug
    name: CategoryName


class OriginalArticleEmbed(_CamelBase):
    """原文記事の参照情報（詳細画面用）"""

    title: str
    url: SafeUrl
