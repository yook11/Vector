"""provider × model 単位の rate limit ドメイン。

公開 API:

- ``RatePolicy``: AI component 側が provider/model/rpm/rpd を表す入力契約 VO
- ``ProviderRateLimitGate``: extraction/assessment/embedding 3 stage 共通の非保持 facade

Redis primitive (``RateLimiter`` / ``RateLimitExceededError``) は ``_redis_limiter``
に隠蔽し、外部からの直接 import は想定しない。
``analysis`` 外で 2 箇所目の caller が現れた時点で ``app/redis/`` 配下への昇格を
検討する (現状はキー設計が AI provider quota に寄っているため本パッケージ内に閉じる)。
"""

from app.analysis.rate_limit.gate import ProviderRateLimitGate
from app.analysis.rate_limit.policy import RatePolicy, RatePolicySource

__all__ = ["ProviderRateLimitGate", "RatePolicy", "RatePolicySource"]
