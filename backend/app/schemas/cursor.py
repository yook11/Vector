"""一覧の続きの位置を表すカーソルの文字列表現。

位置の JSON を base64url (padding なし) にしたもので、クライアントは中身を解釈しない。
位置型は pydantic dataclass で定義する (BaseModel だと FastAPI が任意パラメータの中の
モデルを OpenAPI に載せ、カーソルの中身が公開型になるため)。
"""

import base64
import re
from collections.abc import Callable
from functools import cache
from typing import Any

from pydantic import TypeAdapter, WithJsonSchema

_MAX_CURSOR_LENGTH = 256
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

CURSOR_JSON_SCHEMA = WithJsonSchema({"type": "string"})


@cache
def _adapter(position_type: type) -> TypeAdapter[Any]:
    return TypeAdapter(position_type)


def encode_cursor(position: object) -> str:
    raw = _adapter(type(position)).dump_json(position)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def cursor_decoder[P](position_type: type[P]) -> Callable[[object], P]:
    """カーソル文字列を位置に戻す検証関数を作る。読めない値は 422 にする。"""
    adapter = _adapter(position_type)

    def decode(value: object) -> P:
        if (
            not isinstance(value, str)
            or len(value) > _MAX_CURSOR_LENGTH
            or not _CURSOR_PATTERN.fullmatch(value)
        ):
            raise ValueError("invalid cursor")
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            return adapter.validate_json(raw)
        except ValueError as exc:
            raise ValueError("invalid cursor") from exc

    return decode
