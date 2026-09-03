"""根拠付き回答工程の分類済み失敗。"""

from __future__ import annotations

__all__ = ["EvidenceAnswerError"]


class EvidenceAnswerError(Exception):
    """既知の理由により根拠付き回答を作成できなかった。"""

    def __init__(self, *, code: str) -> None:
        if not code:
            raise ValueError("code must not be empty")
        self.code = code
        super().__init__(code)
