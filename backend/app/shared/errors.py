"""アプリケーションで定義した例外のメッセージと診断情報の共通契約。"""

# 構造を持つ診断情報は、例外の種類ごとの変換と TypedDict で表す。
type ApplicationErrorValue = str | int | float | bool | None


class ApplicationError(Exception):
    """例外側で明示したメッセージと診断情報を保持する。"""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, ApplicationErrorValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details
