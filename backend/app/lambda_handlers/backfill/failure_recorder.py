"""入口の失敗箇所と例外型だけを記録し、元の結果を維持する。"""

import structlog

from app.audit.error_fields import exception_fqn

logger = structlog.get_logger(__name__)


def record_failure(stage: str, phase: str, error: BaseException) -> None:
    try:
        logger.error(
            "backfill_lambda_failed",
            stage=stage,
            phase=phase,
            error_class=exception_fqn(error),
        )
    except Exception:  # noqa: S110
        # 診断の障害で業務例外や終了結果を変更しない。
        pass
