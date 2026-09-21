"""投入Lambdaの失敗を、入力や接続情報を含まない診断へ変換する。"""

from typing import Literal

import structlog

from app.audit.error_fields import exception_fqn

type FailurePhase = Literal["input", "settings", "resources", "dispatch"]

logger = structlog.get_logger(__name__)


class SourceDispatchLambdaError(Exception):
    def __init__(self, *, phase: FailurePhase, error_class: str) -> None:
        super().__init__()
        self.phase = phase
        self.error_class = error_class


def record_failure(phase: FailurePhase, error: Exception) -> SourceDispatchLambdaError:
    failure = SourceDispatchLambdaError(phase=phase, error_class=exception_fqn(error))
    try:
        logger.error(
            "source_dispatch_lambda_failed",
            phase=failure.phase,
            error_class=failure.error_class,
        )
    except Exception:  # noqa: S110 — 診断の失敗で元の失敗を上書きしない。
        pass
    return failure


def record_cleanup_failure(resource: str, error: Exception) -> None:
    try:
        logger.warning(
            "source_dispatch_resources_cleanup_failed",
            resource=resource,
            error_class=exception_fqn(error),
        )
    except Exception:  # noqa: S110 — 診断の失敗で確定済みの結果を変更しない。
        pass
