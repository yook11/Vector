"""provider 単位の rate limit acquire を 1 メソッドに閉じる gate。

extraction / assessment / embedding 3 stage 共通の rate limit facade。
Task / Service が limiter / quota 例外を直接知る代わりに、gate に policy を
渡して acquired/skipped の bool だけ受け取る形に圧縮する。
"""

from __future__ import annotations

from app.analysis.rate_limit.policy import AIModelRateLimitPolicy, RateLimitRule
from app.redis import get_redis
from app.redis.sliding_window import RateLimitExceededError, SlidingWindowLimiter


def _rate_limit_key(policy: AIModelRateLimitPolicy, rule: RateLimitRule) -> str:
    """provider × model × bucket の Redis key を組み立てる。"""
    return f"ratelimit:{policy.provider}:{policy.model}:{rule.name}"


def _build_limiters(
    policy: AIModelRateLimitPolicy,
) -> tuple[SlidingWindowLimiter, ...]:
    """provider × model ごとに独立した rule リミッターを構築する。

    Gemini 公式は rate limit を project × model で適用するため、stage が違っても
    同一 provider × 同一 model を共有する呼び出しは 1 つのカウンタを共有する
    (provider 側の実 quota と整合)。

    Returns:
        ``policy.rules`` と同じ順序で構築した limiter。``rules=()`` なら空 tuple。
    """
    if not policy.rules:
        return ()

    redis = get_redis()
    return tuple(
        SlidingWindowLimiter(
            redis=redis,
            key=_rate_limit_key(policy, rule),
            max_requests=rule.max_requests,
            window_seconds=rule.window_seconds,
            block=rule.block,
        )
        for rule in policy.rules
    )


class ProviderRateLimitGate:
    """``acquire(policy)`` で rule 群の limiter acquire を行う非保持 facade。

    ``_build_limiters`` は provider:model キーで limiter を bind するので、
    gate を 1 インスタンス共有しても、policy が違えば別 limiter になる
    (3 stage を同じ gate で wiring しても干渉しない)。
    """

    async def acquire(self, policy: AIModelRateLimitPolicy) -> bool:
        """policy rule 順に acquire。quota 超過なら ``False`` を返す。

        - ``rules=()`` なら Redis に触らず ``True``。
        - いずれかが quota 超過なら ``RateLimitExceededError`` を catch して
          ``False`` を返す (caller は log + return で skip 動作を選べる)。
        """
        limiters = _build_limiters(policy)
        try:
            for limiter in limiters:
                await limiter.acquire()
        except RateLimitExceededError:
            return False
        return True
