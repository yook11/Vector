"""AI model rate limit policy — provider/model 単位の呼び出し制約。

Gemini 公式は rate limit を project × model で適用するため、アプリ側のキー
名前空間も provider × model で揃える。stage (curate/assess/embed) が同一
モデルを共有する場合でも 1 つのカウンタを共有することで、provider 実 quota
と整合した予算管理になる。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """1 つの rate limit bucket に対する適用ルール。

    ``name`` は Redis key bucket に使う安定識別子 (例: ``"rpd"`` / ``"rpm"``)。
    ``block=False`` の rule が超過した場合、gate は caller に ``False`` を返す。
    ``block=True`` の rule は limiter 側で空きが出るまで待つ。
    """

    name: str
    max_requests: int
    window_seconds: int
    block: bool

    def __post_init__(self) -> None:
        # spec 定義ミスを起動時 / test 時に早く検出する。
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"name must be non-empty str, got {self.name!r}")
        if (
            not isinstance(self.max_requests, int)
            or isinstance(self.max_requests, bool)
            or self.max_requests <= 0
        ):
            raise ValueError(
                f"max_requests must be positive int, got {self.max_requests!r}"
            )
        if (
            not isinstance(self.window_seconds, int)
            or isinstance(self.window_seconds, bool)
            or self.window_seconds <= 0
        ):
            raise ValueError(
                f"window_seconds must be positive int, got {self.window_seconds!r}"
            )
        if not isinstance(self.block, bool):
            raise ValueError(f"block must be bool, got {self.block!r}")


@dataclass(frozen=True, slots=True)
class AIModelRateLimitPolicy:
    """provider × model 粒度の rate limit 呼び出し制約。

    AI call spec が直接保持し、AI component は ``rate_limit_policy`` property で
    本 policy を返す。Redis key などの infra 表現は gate 側で組み立てる。
    制限しないモデルは ``rules=()`` で表す。
    """

    provider: str
    model: str
    rules: tuple[RateLimitRule, ...]

    def __post_init__(self) -> None:
        # spec 定義ミスを起動時 / test 時に早く検出する。
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError(f"provider must be non-empty str, got {self.provider!r}")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError(f"model must be non-empty str, got {self.model!r}")
        if not isinstance(self.rules, tuple):
            raise ValueError(f"rules must be tuple, got {self.rules!r}")
        if any(not isinstance(rule, RateLimitRule) for rule in self.rules):
            raise ValueError("rules must contain only RateLimitRule")
