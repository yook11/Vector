"""Curation工程で確定した失敗理由を保持する。"""

from __future__ import annotations

from enum import StrEnum


class CurationFailureReason(StrEnum):
    """呼び出し側の再試行・記事削除方針から独立した失敗理由。"""

    RESPONSE_INVALID = "response_invalid"


class CurationError(Exception):
    """Curation工程で確定した失敗理由を呼び出し元へ伝える。"""

    def __init__(self, *, reason: CurationFailureReason) -> None:
        if not isinstance(reason, CurationFailureReason):
            raise TypeError("reason must be CurationFailureReason")
        super().__init__()
        self.reason = reason

    @property
    def code(self) -> str:
        """観測用のコードには安全な種別ラベルだけを用いる。"""
        return "extraction_response_invalid"


class CurationResponseInvalidError(CurationError):
    """既存のコードと引数なし契約でAI応答不正を表す。"""

    def __init__(self) -> None:
        super().__init__(reason=CurationFailureReason.RESPONSE_INVALID)
