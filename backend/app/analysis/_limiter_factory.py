"""provider × model 単位の RPM/RPD レートリミッターのファクトリ。

3 stage (extraction / assessment / embedding) の task から共通に呼ばれる
ドメイン helper。Redis 接続と低レベル ``RateLimiter`` を組み合わせて、
provider × model でキー名前空間を分割する。

Gemini 公式は rate limit を project × model で適用するため、stage が違っても
同一 provider × 同一 model を共有する呼び出しは 1 つのカウンタを共有する
(provider 側の実 quota と整合)。キー組み立てロジックは ``RatePolicy`` (VO) が
SSoT。

低レベル primitive は ``app.analysis.rate_limiter.RateLimiter`` 側に閉じている。
"""

from __future__ import annotations

from app.analysis.rate_limiter import RateLimiter
from app.analysis.rate_policy import RatePolicy
from app.redis import get_redis


def _build_limiters(
    policy: RatePolicy,
) -> tuple[RateLimiter | None, RateLimiter | None]:
    """provider × model ごとに独立した RPM/RPD リミッターを構築する。

    Returns:
        (rpm_limiter, rpd_limiter) のタプル。``policy.rpm`` / ``policy.rpd``
        が ``None`` のときは対応する limiter も ``None`` で返る。
    """
    redis = get_redis()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if policy.rpm is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=policy.rpm_key,
            max_requests=policy.rpm,
            window_seconds=60,
            block=True,
        )
    if policy.rpd is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=policy.rpd_key,
            max_requests=policy.rpd,
            window_seconds=86400,
            block=False,
        )
    return rpm_limiter, rpd_limiter
