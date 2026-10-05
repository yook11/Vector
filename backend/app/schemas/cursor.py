"""一覧の続きの位置を表すカーソルの文字列表現。

位置の JSON を base64url (padding なし) にしたもので、クライアントは中身を解釈しない。
位置型は CursorPosition を継承した pydantic dataclass で定義する
(BaseModel だと FastAPI が任意パラメータの中のモデルを OpenAPI に載せ、
カーソルの中身が公開型になるため)。
"""

import base64
import re
from functools import cache
from typing import Any, Self

from pydantic import TypeAdapter, WithJsonSchema

_MAX_CURSOR_LENGTH = 256
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

CURSOR_JSON_SCHEMA = WithJsonSchema({"type": "string"})


@cache
def _adapter(position_type: type) -> TypeAdapter[Any]:
    return TypeAdapter(position_type)


class CursorPosition:
    """カーソルの文字列と相互に変換できる、一覧上の位置。"""

    def to_cursor(self) -> str:
        raw = _adapter(type(self)).dump_json(self)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def from_cursor(cls, value: object) -> Self:
        """カーソル文字列を位置に戻す。読めない値は 422 にする。"""
        if (
            not isinstance(value, str)
            or len(value) > _MAX_CURSOR_LENGTH
            or not _CURSOR_PATTERN.fullmatch(value)
        ):
            raise ValueError("invalid cursor")
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            return _adapter(cls).validate_json(raw)
        except ValueError as exc:
            raise ValueError("invalid cursor") from exc
