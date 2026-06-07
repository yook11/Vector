"""トレンドの値オブジェクトと集約ルート。

公開モデル:
- ``RankedMention``: floor 通過 mention 1 件分の集計結果 (件数 + 派生 hotness_score)
  に文脈 (key_points / related_mentions) を添えたもの
- ``RelatedMention``: anchor mention と同一 key_point 内で一緒に語られた別の固有名
- ``CategoryRankings`` (集約ルート): 1 カテゴリ × 1 集計窓分の 2 ランキング束
- ``TrendsBundle`` (snapshot 永続形): 1 集計窓分の全カテゴリ集約

責務:
- 件数の下限/非負を Pydantic ``Field(ge=...)`` で構造的に強制
  (ランタイム if より構造で守る: feedback_structural_guarantee.md)
- 集約は ``frozen=True`` + ``tuple[...]`` 子コレクションで深く immutable
- ``TrendsBundle.model_dump(mode="json")`` 結果をそのまま JSONB に保存し、
  ``model_validate`` で復元できる (snapshot は 1 単位保存:
  feedback_snapshot_responsibility.md)

集計しきい値はトレンドのドメイン知識として本モジュールに集約する
(``config.py`` は廃止。窓 TZ のみ window 責務に同居させ ``domain/window.py`` に置く)。

hotness_score (API では growthRate として晒す):
  ``(appearance_count - previous_appearance_count)`` を
  ``max(previous_appearance_count, SMOOTHING)`` で割る
  - 前週 0 でも除算回避
  - 前週 < SMOOTHING でも分母が SMOOTHING に置き換わるため burst の過大評価を抑える
"""

from __future__ import annotations

from datetime import date
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.analysis.assessment.domain.result import MentionType
from app.insights.trend_discovery.domain.mention_name import MentionName
from app.models.value_objects.category import CategoryName, CategorySlug

# hot 判定 (伸び率ランキング母集団) と noise floor のしきい値。
# - MIN_CURRENT: floor (これ未満は noise として両ランキングから除外)
# - MIN_PREVIOUS: hot 判定の前週最低件数 (継続トレンド側の条件)
# - NEW_BURST_THRESHOLD: 前週 0 でも現週がこの件数以上なら burst として hot
# - SMOOTHING: hotness_score の分母 smoothing (前週 0 除算回避 + 過大評価防止)
MIN_CURRENT: Final[int] = 5
MIN_PREVIOUS: Final[int] = 2
NEW_BURST_THRESHOLD: Final[int] = 10
SMOOTHING: Final[int] = 2

# 各カテゴリ × 各ランキングの表示上限。生成側の truncate 値 (service.py の `[:N]`) と
# Field(max_length=N) の SSoT を domain 側に集約する。
TOP_N_PER_RANKING: Final[int] = 5
MAX_CATEGORIES_PER_BUNDLE: Final[int] = 20

# mention 1 件に添える文脈の上限。
# - MAX_KEY_POINTS_PER_MENTION: 何が言われているか (key_point content) の本数
# - MAX_RELATED_MENTIONS: 何と一緒に語られるか (related mention) の件数
# - MIN_SHARED_ARTICLES: 1 記事だけの共起は noise として除外する閾値
MAX_KEY_POINTS_PER_MENTION: Final[int] = 2
MAX_RELATED_MENTIONS: Final[int] = 3
MIN_SHARED_ARTICLES: Final[int] = 2

# key_point の記事レベル重複間引き閾値 (cosine 距離)。embedding は assessment(記事)
# 単位の 1 本なので、距離がこの値未満の content は同一記事/同一トピックとして畳む。
# 保守的初期値 (ほぼ同一のみ間引く)。実データで調整する。
KEY_POINT_DEDUP_DISTANCE: Final[float] = 0.1

# count フィールドの現実的な上限。anomaly 検出と response DoS 防御を兼ねる。
# 1 カテゴリ × 1 週で 10_000 mention を超える単一 mention/topic は実運用では
# 起こらない (生成側 SQL の集計対象 article 数自体が桁違いに少ない)。
_MAX_COUNT: Final[int] = 10_000


def _hotness(current: int, previous: int) -> float:
    return (current - previous) / max(previous, SMOOTHING)


class RelatedMention(BaseModel):
    """anchor mention と同一 key_point 内で一緒に語られた別の固有名 1 件。

    関連の強さは ``shared_article_count`` (一緒に語られた記事数) で表す。
    「関連」という意味で名付け、共起判定の機構は名前に出さない (将来 関連の
    出し方を変えても API 名が嘘にならないようにする)。

    Invariants (Pydantic Field 制約):
    - ``shared_article_count >= MIN_SHARED_ARTICLES`` (1 記事だけの共起は noise)
    - frozen
    """

    model_config = ConfigDict(frozen=True)

    name: MentionName
    type: MentionType
    shared_article_count: int = Field(ge=MIN_SHARED_ARTICLES, le=_MAX_COUNT)


class RankedMention(BaseModel):
    """floor 通過 mention 1 件分の集計結果 + 文脈。

    出現回数ランキングと伸び率ランキングで同じ型を共有する (どちらに載るかは
    service 側の並べ替えで決まる)。``key_points`` / ``related_mentions`` は enrich
    前の純集計段階では空 (``default=()``) で、service が ``model_copy`` で後付けする。

    Invariants (Pydantic Field 制約):
    - ``appearance_count >= MIN_CURRENT`` (noise floor)
    - ``previous_appearance_count >= 0``
    - key_point は最大 ``MAX_KEY_POINTS_PER_MENTION`` 本 (content 文字列のみ)
    - related mention は最大 ``MAX_RELATED_MENTIONS`` 件
    - frozen
    """

    model_config = ConfigDict(frozen=True)

    name: MentionName
    type: MentionType
    appearance_count: int = Field(ge=MIN_CURRENT, le=_MAX_COUNT)
    previous_appearance_count: int = Field(ge=0, le=_MAX_COUNT)
    key_points: tuple[str, ...] = Field(
        default=(), max_length=MAX_KEY_POINTS_PER_MENTION
    )
    related_mentions: tuple[RelatedMention, ...] = Field(
        default=(), max_length=MAX_RELATED_MENTIONS
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hotness_score(self) -> float:
        return _hotness(self.appearance_count, self.previous_appearance_count)


class CategoryRankings(BaseModel):
    """1 カテゴリ × 1 集計窓分の 2 ランキング束 (集約ルート)。

    ``most_mentioned``: 出現回数降順 top5 (floor のみ通過した全 mention が母集団)。
    ``fastest_growing``: 伸び率 (hotness) 降順 top5 (floor + hot ゲート通過 mention)。

    集約配下のリストは ``tuple[...]`` で保持し、変更不可性を構造で保証する
    (feedback_aggregate_over_individual_vo.md)。
    """

    model_config = ConfigDict(frozen=True)

    category_id: int
    category_slug: CategorySlug
    category_name: CategoryName
    most_mentioned: tuple[RankedMention, ...] = Field(max_length=TOP_N_PER_RANKING)
    fastest_growing: tuple[RankedMention, ...] = Field(max_length=TOP_N_PER_RANKING)


class TrendsBundle(BaseModel):
    """1 集計窓分の全カテゴリトレンドをまとめた snapshot 永続形。

    ``model_dump(mode="json")`` 出力をそのまま JSONB に保存する。
    snapshot は 1 単位保存が責務 (feedback_snapshot_responsibility.md)。

    ``window_end``: rolling 7d window の上限 (半開区間
    ``[window_end - 7d, window_end)`` の上端、JST 日付)。
    """

    model_config = ConfigDict(frozen=True)

    window_end: date
    sections: tuple[CategoryRankings, ...] = Field(max_length=MAX_CATEGORIES_PER_BUNDLE)
