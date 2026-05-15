"""provider 単位の rate limit acquire を 1 メソッドに閉じる gate。

extraction / assessment / embedding 3 stage 共通の rate limit facade。
Task / Service が rate policy / limiter / quota 例外を直接知る代わりに、
gate に policy を渡して acquired/skipped の bool だけ受け取る形に圧縮する。

低レベル primitive (Redis ZSET + Lua) は ``_redis_limiter`` に閉じ、本 module
からは ``RateLimiter`` / ``RateLimitExceededError`` を import するのみ。
"""

from __future__ import annotations

from app.analysis.rate_limit._redis_limiter import RateLimiter, RateLimitExceededError
from app.analysis.rate_limit.policy import RatePolicy
from app.redis import get_redis


def _build_limiters(
    policy: RatePolicy,
) -> tuple[RateLimiter | None, RateLimiter | None]:
    """provider × model ごとに独立した RPM/RPD リミッターを構築する。

    Gemini 公式は rate limit を project × model で適用するため、stage が違っても
    同一 provider × 同一 model を共有する呼び出しは 1 つのカウンタを共有する
    (provider 側の実 quota と整合)。キー組み立てロジックは ``RatePolicy`` (VO) が
    SSoT。

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


class ProviderRateLimitGate:
    """``acquire(policy)`` で 2 段 limiter acquire を行う非保持 facade。

    ``_build_limiters`` は provider:model キーで limiter を bind するので、
    gate を 1 インスタンス共有しても、policy が違えば別 limiter になる
    (3 stage を同じ gate で wiring しても干渉しない)。
    """

    async def acquire(self, policy: RatePolicy) -> bool:
        """RPD → RPM の順に acquire。quota 超過なら ``False`` を返す。

        - 両 limiter とも ``None`` (RPM=None かつ RPD=None) なら ``True``。
        - いずれかが quota 超過なら ``RateLimitExceededError`` を catch して
          ``False`` を返す (caller は log + return で skip 動作を選べる)。
        """
        rpm_limiter, rpd_limiter = _build_limiters(policy)
        try:
            if rpd_limiter is not None:
                await rpd_limiter.acquire()
            if rpm_limiter is not None:
                await rpm_limiter.acquire()
        except RateLimitExceededError:
            return False
        return True
