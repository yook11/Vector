"""役割別 RPM/RPD レートリミッターのファクトリ。

3 stage (extraction / assessment / embedding) の task から共通に呼ばれる
ドメイン helper。Redis 接続と低レベル ``RateLimiter`` を組み合わせて、
役割 (extract / assess / embed) × モデル名でキー名前空間を分割する。

低レベル primitive は ``app.analysis.rate_limiter.RateLimiter`` 側に閉じている。
"""

from __future__ import annotations

from typing import Literal

from app.analysis.rate_limiter import RateLimiter
from app.redis import get_redis


def _build_limiters(
    role: Literal["extract", "assess", "embed"],
    model: str,
    rpm: int | None,
    rpd: int | None,
) -> tuple[RateLimiter | None, RateLimiter | None]:
    """役割 (extract/assess/embed) ごとに独立した RPM/RPD リミッターを構築する。

    role を Redis キーに含めることで、同一モデルを複数役割で使う場合でも
    レート制御カウンターが共有されない。

    Returns:
        (rpm_limiter, rpd_limiter) のタプル。どちらも None になりうる。
    """
    redis = get_redis()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if rpm is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{role}:{model}:rpm",
            max_requests=rpm,
            window_seconds=60,
            block=True,
        )
    if rpd is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{role}:{model}:rpd",
            max_requests=rpd,
            window_seconds=86400,
            block=False,
        )
    return rpm_limiter, rpd_limiter
