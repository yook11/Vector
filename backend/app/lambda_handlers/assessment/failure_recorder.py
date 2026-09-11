"""Assessmentの資源準備・終了の失敗を、安全な項目だけで記録する。"""

from structlog.stdlib import BoundLogger

from app.audit.error_fields import exception_fqn


class AssessmentLambdaFailureRecorder:
    """診断の障害で元の例外や利用結果を変更しない。"""

    def __init__(self, logger: BoundLogger) -> None:
        self._logger = logger

    def record_initialization_failure(self, stage: str, error: Exception) -> None:
        self._record(
            "assessment_initialization_failed",
            stage=stage,
            error_class=exception_fqn(error),
        )

    def record_cleanup_failure(self, resource: str, error: Exception) -> None:
        self._record(
            "assessment_resources_cleanup_failed",
            resource=resource,
            error_class=exception_fqn(error),
        )

    def _record(self, event: str, **fields: object) -> None:
        try:
            self._logger.warning(event, **fields)
        except Exception:  # noqa: S110
            # 診断障害で利用結果や元の例外を置き換えない。
            pass
