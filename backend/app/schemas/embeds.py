"""他の API レスポンスに埋め込まれる軽量スキーマ群。

これらのクラスはトップレベルの API レスポンスにはならない。
常に親レスポンススキーマ（NewsBrief, CategoryDetail など）内に
ネストされて利用される。
"""

from app.domain.news_source import SourceName
from app.domain.safe_url import SafeUrl
from app.domain.topic import TopicName
from app.schemas.base import _CamelBase


class TopicEmbed(_CamelBase):
    """トピックタグ（ニュース埋め込み用）"""

    name: TopicName


class TopicStatEmbed(_CamelBase):
    """トピック＋記事数（カテゴリ内集計表示用）"""

    name: TopicName
    article_count: int = 0


class NewsSourceEmbed(_CamelBase):
    """ニュースソースの基本参照情報（フィルタ・表示用）"""

    name: SourceName


class OriginalArticleEmbed(_CamelBase):
    """原文記事の参照情報（詳細画面用）"""

    title: str
    url: SafeUrl
