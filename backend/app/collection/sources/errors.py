"""source registry / source 定義に関する domain error。"""

from __future__ import annotations


class SourceNotRegisteredError(Exception):
    """source registry に対象 source が登録されていない。"""

    MESSAGE = "source is not registered"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)
