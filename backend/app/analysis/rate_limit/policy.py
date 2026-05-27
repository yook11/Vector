"""AI provider rate limit policy — provider/model 単位の設定 VO。

Gemini 公式は rate limit を project × model で適用するため、アプリ側のキー
名前空間も provider × model で揃える。stage (extract/assess/embed) が同一
モデルを共有する場合でも 1 つのカウンタを共有することで、provider 実 quota
と整合した予算管理になる。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RatePolicy:
    """provider × model 粒度の rate limit 設定値オブジェクト。

    AI call spec が直接保持し、AI component は ``rate_policy`` property で
    本 VO を返す。Redis key などの infra 表現は gate 側で組み立てる。
    """

    provider: str
    model: str
    rpm: int | None
    rpd: int | None

    def __post_init__(self) -> None:
        # spec 定義ミスを起動時 / test 時に早く検出する。
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError(f"provider must be non-empty str, got {self.provider!r}")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError(f"model must be non-empty str, got {self.model!r}")
        if self.rpm is not None and (not isinstance(self.rpm, int) or self.rpm <= 0):
            raise ValueError(f"rpm must be None or positive int, got {self.rpm!r}")
        if self.rpd is not None and (not isinstance(self.rpd, int) or self.rpd <= 0):
            raise ValueError(f"rpd must be None or positive int, got {self.rpd!r}")
