"""他の API レスポンスに埋め込まれる軽量スキーマ群。

これらのクラスはトップレベルの API レスポンスにはならない。
常に親レスポンススキーマ（NewsBrief, CategoryDetail など）内に
ネストされて利用される。
"""

from app.domain.keyword import KeywordName
from app.domain.news_source import SourceName
from app.domain.safe_url import SafeUrl
from app.schemas.base import _CamelBase


class KeywordEmbed(_CamelBase):
    """キーワードタグ（ニュース埋め込み用）"""

    name: KeywordName


class KeywordStatEmbed(_CamelBase):
    """キーワード＋記事数（カテゴリ内集計表示用）"""

    name: KeywordName
    article_count: int = 0


class NewsSourceEmbed(_CamelBase):
    """ニュースソースの基本参照情報（フィルタ・表示用）"""

    name: SourceName


class OriginalArticleEmbed(_CamelBase):
    """原文記事の参照情報（詳細画面用）"""

    title: str
    url: SafeUrl
    content: str | None = None
