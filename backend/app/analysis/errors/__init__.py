"""Analysis 系 error 階層の集約 export (旧 ``app/analysis/errors.py`` の後継)。

PR3.5-a で ``errors.py`` を package に転換し、Layer 2-A (provider) / Layer 2-B
(extraction 等) を別ファイルに分離した。本 module は旧 ``errors.py`` 互換のため
全 type を re-export する。

旧 8 type (``AnalysisDomainError`` 階層) は PR3.5-c で raise/catch 側が新型に切替、
PR3.5-d で本ファイルから定義削除予定。本 PR (PR3.5-a) では動作変化なし — 新型は
追加するだけで誰も raise しない。

詳細: ``specs/pipeline-events-error-taxonomy.md``
"""

from __future__ import annotations

# Layer 2-B Stage 3: Extraction 固有 (新規、PR3.5-c で raise 開始)
from app.analysis.errors.extraction import (
    ExtractionDomainError,
    ExtractionResponseInvalidError,
)

# Layer 2-A: AI provider 由来 (新規、PR3.5-c で raise 開始)
from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)

# ---------------------------------------------------------------------------
# Legacy types (旧 errors.py の内容を移植、PR3.5-c/d で段階削除予定)
# ---------------------------------------------------------------------------


class AnalysisDomainError(Exception):
    """Analysis ドメイン全般のエラー基底（analyzer と embedder の両方を含む）。"""


class InvalidInputError(AnalysisDomainError):
    """入力側の問題（不正なプロンプト、長すぎる等）。該当記事をスキップする。"""


class ConfigurationError(AnalysisDomainError):
    """設定または認証の問題。全処理を停止し運用者に通知する。"""


class ProviderError(AnalysisDomainError):
    """プロバイダー側の問題（Google の 5xx、壊れたレスポンス等）。後でリトライする。"""


class NetworkError(AnalysisDomainError):
    """通信の問題（タイムアウト、接続拒否等）。後でリトライする。"""


class RateLimitError(AnalysisDomainError):
    """レート制限の超過（HTTP 429 / RESOURCE_EXHAUSTED）。待機してからリトライする。"""


class DailyQuotaExhaustedError(AnalysisDomainError):
    """1 日あたりのリクエスト上限（RPD）到達。翌日まで停止する。"""


class InsufficientBalanceError(AnalysisDomainError):
    """プロバイダーの残高不足（DeepSeek の HTTP 402 等）。

    ConfigurationError と同様に、リトライしても解消しないため task は no-retry に
    回す。退避は brokers.py の composition root を別アダプターに差し替える
    コード変更（PR）で行う。
    """


class UnclassifiedError(AnalysisDomainError):
    """原因不明。ログに残して調査する。"""


__all__ = [
    # Legacy (旧 errors.py)
    "AnalysisDomainError",
    "InvalidInputError",
    "ConfigurationError",
    "ProviderError",
    "NetworkError",
    "RateLimitError",
    "DailyQuotaExhaustedError",
    "InsufficientBalanceError",
    "UnclassifiedError",
    # Layer 2-A
    "AIProviderError",
    "AIProviderConfigurationError",
    "AIProviderRequestInvalidError",
    "AIProviderInsufficientBalanceError",
    "AIProviderRateLimitedError",
    "AIProviderQuotaExhaustedError",
    "AIProviderServiceUnavailableError",
    "AIProviderNetworkError",
    "AIProviderInputRejectedError",
    "AIProviderOutputBlockedError",
    # Layer 2-B Stage 3
    "ExtractionDomainError",
    "ExtractionResponseInvalidError",
]
