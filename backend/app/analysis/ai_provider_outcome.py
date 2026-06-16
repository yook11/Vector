"""provider error の health-attribution 分類 (metric 集計用、stage 中立)。

processing_outcome metric が provider 由来失敗を ``infra_error`` (成功率の分母外) と
``failed`` (分母に算入) に振り分けるための述語。domain の retry 軸
(``AIProviderFailureMode``) や class 系統 (``AIProviderStateError`` /
``AIProviderContentError``) とは一致しないため、metric 専用の分類をここに持つ。入力は
stage 非依存の provider error なので、ドメインエラー class に bucket を生やさず
consumer 層に置く。
"""

from __future__ import annotations

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)

# 環境・設定・課金・依存先で直る失敗 (成功率の分母外)。
_INFRA_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
)

# stage 自身のコード・リクエスト・対象内容が原因の失敗 (成功率の分母に算入)。
_FAILED_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderRequestInvalidError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


def is_infra_provider_error(exc: AIProviderError | None) -> bool:
    """provider error が infra 起因 (成功率の分母外) か。

    infra -> True / failed -> False / 未分類・None -> False (= failed)。両 set は全
    provider error leaf を網羅し (網羅テストで固定)、未分類の新 leaf は silent に
    infra へ倒さず failed に出す。
    """
    if isinstance(exc, _INFRA_PROVIDER_ERRORS):
        return True
    if isinstance(exc, _FAILED_PROVIDER_ERRORS):
        return False
    return False
