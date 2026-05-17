"""Source 補完ポリシー — per-source の補完規則 (composition root 純データ)。

「どのフィールドを HTML 補完で正本とするか」は記事インスタンスの状態ではなく
**ソースの出自 (provenance) で構造的に決まる** (spec §1.2/§4.2)。RSS は body を
構造的に欠き、sitemap 系は title を欠く。これはソース種別の capability であって
記事ごとの自由状態ではないため、ポリシーを ``SourceCompletionProfile`` に集約し
``Source`` 集約が所有する。

``FieldCompletionPolicy`` は payload を持たない 3 値マーカーであり JSONB にも
wire にも乗らない (profile は非永続)。よって discriminated union ではなく
``StrEnum`` で表現する (Wlaschin "when NOT to use a DU": 空マーカーの DU は
ceremony が増えるだけの enum)。網羅は ``match`` + ``assert_never`` で型検査器に
強制させる (DU と同じ規律)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class AnalyzableField(StrEnum):
    """``AnalyzableArticle`` のうち補完対象となるフィールド。

    ``source_id`` / ``source_url`` は identity であり常在するため対象外。
    """

    title = "title"
    body = "body"
    published_at = "published_at"


class FieldCompletionPolicy(StrEnum):
    """フィールド単位の補完正本ルール。

    - ``html_required``: 観測値なし前提・HTML が正本・両欠で失敗。
    - ``html_preferred``: 観測値があっても HTML を正本 (sitemap/listing 系の
      仮タイトル特例)。
    - ``observed_preferred``: 観測値が勝ち・HTML は fallback (旧 published_at hint)。
    """

    html_required = "html_required"
    html_preferred = "html_preferred"
    observed_preferred = "observed_preferred"


@dataclass(frozen=True, slots=True)
class SourceCompletionProfile:
    """全 ``AnalyzableField`` → policy の全域写像。

    3 frozenset 分割案は「同一 field が複数集合に入る矛盾」を構造的に防げない
    ため却下 (spec §9)。全域 policy map にして矛盾を型で不能化する。

    ``policies`` は ``__post_init__`` で全域性を検証したうえ
    ``MappingProxyType`` でコピー固定し、生成後の内容変更を構造的に封じる
    (frozen dataclass でも内包 dict は可変なため)。
    """

    policies: Mapping[AnalyzableField, FieldCompletionPolicy]

    def __post_init__(self) -> None:
        missing = set(AnalyzableField) - set(self.policies)
        if missing:
            msg = f"profile missing policy for {sorted(f.value for f in missing)}"
            raise ValueError(msg)
        object.__setattr__(self, "policies", MappingProxyType(dict(self.policies)))


# 大多数のソース: title/published_at は観測 (RSS) を正本に、body のみ HTML 必須。
DEFAULT_PROFILE = SourceCompletionProfile(
    {
        AnalyzableField.title: FieldCompletionPolicy.observed_preferred,
        AnalyzableField.body: FieldCompletionPolicy.html_required,
        AnalyzableField.published_at: FieldCompletionPolicy.observed_preferred,
    }
)

# sitemap / listing 系 (Anthropic / ORNL): RSS が真の title を持たず HTML 側が
# 正本。旧 sitemap/listing 仮タイトル特例の構造的後継 (spec §3.3/§4.2)。
HTML_TITLE_PROFILE = SourceCompletionProfile(
    {
        AnalyzableField.title: FieldCompletionPolicy.html_preferred,
        AnalyzableField.body: FieldCompletionPolicy.html_required,
        AnalyzableField.published_at: FieldCompletionPolicy.observed_preferred,
    }
)
