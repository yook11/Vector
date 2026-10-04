"""他の API レスポンスに埋め込まれる軽量スキーマ群。

これらのクラスはトップレベルの API レスポンスにはならない。
常に親レスポンススキーマ（ArticleBrief, BriefingDetail など）内に
ネストされて利用される。
"""

from app.collection.sources.source_name import SourceName
from app.schemas.base import _CamelBase
from app.shared.web_url import WebUrl


class NewsSourceEmbed(_CamelBase):
    """ニュースソースの基本参照情報（フィルタ・表示用）"""

    name: SourceName
    attribution_label: str | None = None


class OriginalArticleEmbed(_CamelBase):
    """原文記事の参照情報（詳細画面用）"""

    title: str
    url: WebUrl
