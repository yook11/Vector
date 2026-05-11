"""Layer 2-A: AI provider 呼び出し由来のエラー 9 種。

extractor / assessor / embedder の client (Gemini / DeepSeek) が provider 例外を
ここに翻訳する。Layer 1 marker (``RetryableError`` / ``NonRetryableDropArticle`` /
``NonRetryableKeepArticle``) と多重継承して dispatch 軸を表現する。

- ``CODE``: ``pipeline_events.code`` カラムへ直接書き込む文字列 (型 SSoT)。
- ``INLINE_RETRY``: ``RetryableError`` 系のみ pin。``True`` なら taskiq の即時
  retry に乗せる。

詳細: ``specs/pipeline-events-error-taxonomy.md`` §Layer 2-A
"""

from __future__ import annotations

from typing import ClassVar

from app.observability.categories import (
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)


class AIProviderError(Exception):
    """AI provider 由来エラーの共通祖先 (Layer 2-A 識別 marker)。

    具体型は本クラスと Layer 1 marker の **多重継承** で定義する。MRO 規約: 左に
    origin (本クラス)、右に dispatch marker。
    """


# ---------------------------------------------------------------------------
# NonRetryableDropArticle: provider が明示的に処理拒否したケースのみ (2 種)
# ---------------------------------------------------------------------------


class AIProviderInputRejectedError(AIProviderError, NonRetryableDropArticle):
    """provider が入力を明示的に拒否した。

    policy 違反 / token 超過 / 入力 safety block 等。
    """

    CODE: ClassVar[str] = "ai_error_input_rejected"


class AIProviderOutputBlockedError(AIProviderError, NonRetryableDropArticle):
    """provider が応答を blocked-by-safety / recitation 等で抑制した。"""

    CODE: ClassVar[str] = "ai_error_output_blocked"


# ---------------------------------------------------------------------------
# NonRetryableKeepArticle: 運用側修正が必要 (記事は健全) (3 種)
# ---------------------------------------------------------------------------


class AIProviderConfigurationError(AIProviderError, NonRetryableKeepArticle):
    """API key 不正 / model 名不正 / endpoint misconfig 等。運用者対応で復旧。"""

    CODE: ClassVar[str] = "ai_error_configuration"


class AIProviderRequestInvalidError(AIProviderError, NonRetryableKeepArticle):
    """request 構造が provider 仕様に合致しない (caller 側 bug)。"""

    CODE: ClassVar[str] = "ai_error_request_invalid"


class AIProviderInsufficientBalanceError(AIProviderError, NonRetryableKeepArticle):
    """残高不足 (DeepSeek HTTP 402 等)。アダプター差し替え or 課金で復旧。"""

    CODE: ClassVar[str] = "ai_error_insufficient_balance"


# ---------------------------------------------------------------------------
# RetryableError: 一時障害 (4 種)
# ---------------------------------------------------------------------------


class AIProviderRateLimitedError(AIProviderError, RetryableError):
    """rate limit (HTTP 429 / RESOURCE_EXHAUSTED)。

    inline retry はしない (近 tick で救済不可)。
    """

    CODE: ClassVar[str] = "ai_error_rate_limited"
    INLINE_RETRY: ClassVar[bool] = False


class AIProviderQuotaExhaustedError(AIProviderError, RetryableError):
    """日次 quota (RPD) 到達。翌日まで inline retry しない。"""

    CODE: ClassVar[str] = "ai_error_quota_exhausted"
    INLINE_RETRY: ClassVar[bool] = False


class AIProviderServiceUnavailableError(AIProviderError, RetryableError):
    """provider 一時障害 (HTTP 5xx)。inline retry する。"""

    CODE: ClassVar[str] = "ai_error_service_unavailable"
    INLINE_RETRY: ClassVar[bool] = True


class AIProviderNetworkError(AIProviderError, RetryableError):
    """通信障害 (timeout / connection refused / DNS 失敗等)。inline retry する。"""

    CODE: ClassVar[str] = "ai_error_network"
    INLINE_RETRY: ClassVar[bool] = True
