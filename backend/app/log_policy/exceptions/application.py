"""アプリの共通例外が保持するメッセージと診断情報を受け渡す。"""

from app.log_policy.exceptions.types import ConvertedException
from app.shared.errors import ApplicationError


def convert_application_error(exc: ApplicationError) -> ConvertedException:
    """例外側で明示したメッセージと診断情報を共通形式へ写す。"""
    try:
        return ConvertedException(message=str(exc), error_details=exc.details)
    except Exception:
        return ConvertedException(message="[exception message unavailable]")
