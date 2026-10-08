"""共有ライフサイクルの記録境界をCurationのログへ接続する。"""

from structlog.typing import FilteringBoundLogger


class CurationLambdaFailureRecorder:
    """初期化と資源解放の通知を、呼び出し文脈付きで共通記録口へ渡す。"""

    def __init__(self, logger: FilteringBoundLogger) -> None:
        self._logger = logger

    def record_initialization_failure(self, stage: str, error: Exception) -> None:
        self._logger.error(
            "curation_initialization_failed",
            operation=stage,
            exc_info=error,
        )

    def record_cleanup_failure(self, resource: str, error: Exception) -> None:
        self._logger.error(
            "curation_resources_cleanup_failed",
            operation="cleanup",
            resource=resource,
            exc_info=error,
        )
