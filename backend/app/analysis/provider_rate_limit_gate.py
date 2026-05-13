"""provider 単位の rate limit acquire を 1 メソッドに閉じる gate。

Task / Service が rate policy / limiter / quota 例外を直接知る代わりに、
gate に policy を渡して acquired/skipped の bool だけ受け取る形に圧縮する。

Stage 3 (extraction) の wiring を本 PR で先行切替し、Stage 4 (assessment) /
Stage 5 (embedding) も後続 PR で同じ gate に寄せる前提の API にする。配置を
``app.analysis`` 直下に置くのは、Stage 4/5 が後で extraction sub-package を
import する依存方向を作らないため。
"""

from __future__ import annotations

from app.analysis._limiter_factory import _build_limiters
from app.analysis.rate_limiter import RateLimitExceededError
from app.analysis.rate_policy import RatePolicy


class ProviderRateLimitGate:
    """``acquire(policy)`` で 2 段 limiter acquire を行う非保持 facade。

    ``_build_limiters`` は provider:model キーで limiter を bind するので、
    gate を 1 インスタンス共有しても、policy が違えば別 limiter になる
    (Stage 4/5 を後続 PR で寄せても干渉しない)。
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
