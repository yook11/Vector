"""他の API レスポンスに埋め込まれる軽量スキーマ群。

これらのクラスはトップレベルの API レスポンスにはならない。
常に親レスポンススキーマ（NewsBrief, CategoryDetail など）内に
ネストされて利用される。
"""

from app.analysis.domain.value_objects.topic import TopicName
from app.collection.domain.value_objects.source import SourceName
from app.schemas.base import _CamelBase
from app.shared.value_objects.safe_url import SafeUrl


class TopicEmbed(_CamelBase):
    """トピックタグ（ニュース埋め込み用）"""

    name: TopicName
    label_ja: str


class TopicStatEmbed(_CamelBase):
    """トピック＋直近24時間に AI 分類が完了した記事数（カテゴリ内集計表示用）"""

    name: TopicName
    label_ja: str
    recent_count: int = 0


class NewsSourceEmbed(_CamelBase):
    """ニュースソースの基本参照情報（フィルタ・表示用）"""

    name: SourceName


class OriginalArticleEmbed(_CamelBase):
    """原文記事の参照情報（詳細画面用）"""

    title: str
    url: SafeUrl
