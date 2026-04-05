"""Domain exceptions raised by the service layer.

Each exception type maps to a specific HTTP status code.
The router layer catches these and converts to HTTPException.
"""


class NotFoundError(Exception):
    """Target resource does not exist → 404."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class DuplicateError(Exception):
    """Unique constraint would be violated → 409."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ReferenceNotFoundError(Exception):
    """Referenced entity does not exist → 400."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)
