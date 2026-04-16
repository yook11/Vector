"""検証済み HTTP/HTTPS URL の値オブジェクト。

SafeUrl は URL が安全なスキーム (http または https) を使い、
正しい構造を持つことを保証する。検証は Pydantic の AnyHttpUrl に
委譲するが、格納される値は元の文字列 (strip 後) で、
正規化は行わない。
"""

from __future__ import annotations

from typing import Any

from pydantic import (
    AnyHttpUrl,
    ConfigDict,
    RootModel,
    TypeAdapter,
    ValidationError,
    field_validator,
)

_url_adapter = TypeAdapter(AnyHttpUrl)
_MAX_LENGTH = 2048


class SafeUrl(RootModel[str]):
    """Pydantic によって検証された HTTP/HTTPS URL。

    Invariants:
    - http または https スキームを使用
    - 有効な URL 構造 (最低でも scheme + host)
    - トリム後 1-2048 文字
    - 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        if not isinstance(v, str):
            msg = f"Expected str, got {type(v).__name__}"
            raise ValueError(msg)
        v = v.strip()
        if not v:
            msg = "SafeUrl must not be empty"
            raise ValueError(msg)
        if len(v) > _MAX_LENGTH:
            msg = f"SafeUrl must be at most {_MAX_LENGTH} chars"
            raise ValueError(msg)
        try:
            _url_adapter.validate_python(v)
        except ValidationError:
            msg = "SafeUrl must be a valid http or https URL"
            raise ValueError(msg) from None
        return v

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"SafeUrl({self.root!r})"
