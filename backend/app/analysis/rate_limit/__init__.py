"""provider × model 単位の rate limit ドメイン。

公開 API:

- ``RatePolicy``: AI call spec 側が provider/model/rpm/rpd を表す設定 VO
- ``ProviderRateLimitGate``: extraction/assessment/embedding 3 stage 共通の非保持 facade
"""

from app.analysis.rate_limit.gate import ProviderRateLimitGate
from app.analysis.rate_limit.policy import RatePolicy

__all__ = ["ProviderRateLimitGate", "RatePolicy"]
