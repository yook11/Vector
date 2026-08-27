"""Direct Answer工程の分類済み失敗。"""

from __future__ import annotations

__all__ = ["DirectAnswerError"]


class DirectAnswerError(Exception):
    """既知の理由によりDirect Answerを作成できなかった。"""

    def __init__(self, *, code: str) -> None:
        if not code:
            raise ValueError("code must not be empty")
        self.code = code
        super().__init__(code)
