"""Analysis ドメインのエラー階層。

原因の所在で分類することで、受け取り側が「何が起きたか」「誰の問題か」を
即座に判断できるようにする。
"""


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


class UnclassifiedError(AnalysisDomainError):
    """原因不明。ログに残して調査する。"""
