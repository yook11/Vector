"""投入の実行の終わり方と資源解放の失敗を、工程制御のログ方針の形で記録する。"""

from typing import Literal

from structlog.typing import FilteringBoundLogger

from app.collection.article_acquisition.acquisition_dispatch import (
    AcquisitionDispatchError,
)

type DispatchOperation = Literal["validate_event", "settings", "resources", "dispatch"]


class SourceDispatchRecorder:
    """実行の終わり方ごとに、終端ログのイベント名・レベル・項目を決める。"""

    def __init__(self, logger: FilteringBoundLogger) -> None:
        self._logger = logger

    def record_completed(self, sent_count: int, *, duration_ms: float) -> None:
        self._logger.info(
            "source_dispatch_completed", sent_count=sent_count, duration_ms=duration_ms
        )

    def record_failed(
        self, operation: DispatchOperation, error: Exception, *, duration_ms: float
    ) -> None:
        investigate = (
            error.requires_investigation
            if isinstance(error, AcquisitionDispatchError)
            else True
        )
        # 人の調査が要る失敗だけをerrorにし、想定内の失敗はwarningにする。
        (self._logger.error if investigate else self._logger.warning)(
            "source_dispatch_failed",
            operation=operation,
            requires_investigation=investigate,
            duration_ms=duration_ms,
            exc_info=error,
        )

    def record_cleanup_failure(self, resource: str, error: Exception) -> None:
        self._logger.warning(
            "source_dispatch_resources_cleanup_failed",
            resource=resource,
            exc_info=error,
        )
