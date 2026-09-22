"""アプリケーションで定義した例外のメッセージと診断情報の共通契約。"""

type ApplicationErrorValue = (
    str
    | int
    | float
    | bool
    | None
    | list[ApplicationErrorValue]
    | dict[str, ApplicationErrorValue]
)


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
