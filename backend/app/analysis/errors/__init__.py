"""Legacy Analysis ドメインエラー 8 種の一時保持 (PR3.5-d で削除予定)。

Stage 3 Layer 2-B エラーは ``app/analysis/extraction/errors.py`` (Stage 4/5 と
対称な位置) に、AI provider 由来エラー 9 種は ``app/analysis/ai_provider_errors.py``
(``analysis/`` 直下) に分離済。本 file は ``AnalysisDomainError`` 階層を raise/catch
する旧コード (``re_extraction_service.py`` 等) が残っている間の一時的な保持場所で
あり、PR3.5-d で legacy raise/catch を新型に切替えた上で本 file ごと削除する。

詳細: ``specs/pipeline-events-error-taxonomy.md``
"""

from __future__ import annotations


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
    "AnalysisDomainError",
    "InvalidInputError",
    "ConfigurationError",
    "ProviderError",
    "NetworkError",
    "RateLimitError",
    "DailyQuotaExhaustedError",
    "InsufficientBalanceError",
    "UnclassifiedError",
]
