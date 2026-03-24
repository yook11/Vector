"""Value objects for the Keyword entity.

KeywordName: A tag representing a specific technology or theme within a sector.
Examples: "large language model", "AI/ML", "C++", "Node.js", "量子エラー訂正"
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema


class KeywordName:
    """Tag name for a technology or theme within a sector.

    Invariants:
    - Contains at least one word character (\\w)
    - Only word chars (Unicode), spaces, hyphens, dots, &, /, +, #
    - 1-100 characters after trimming
    - Immutable after creation
    """

    __slots__ = ("_value",)
    _PATTERN = re.compile(r"^(?=.*\w)[\w \-\.&/+#]+$", re.UNICODE)
    _MAX_LENGTH = 100

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            msg = f"Expected str, got {type(value).__name__}"
            raise TypeError(msg)
        v = value.strip()
        if not v:
            msg = "KeywordName must not be empty"
            raise ValueError(msg)
        if len(v) > self._MAX_LENGTH:
            msg = f"KeywordName must be at most {self._MAX_LENGTH} chars, got {len(v)}"
            raise ValueError(msg)
        if not self._PATTERN.fullmatch(v):
            msg = (
                "KeywordName can only contain letters, numbers, spaces, "
                "hyphens, dots, &, /, +, #, and underscores. "
                f"Got: {v!r}"
            )
            raise ValueError(msg)
        object.__setattr__(self, "_value", v)

    @property
    def value(self) -> str:
        return self._value

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("KeywordName is immutable")

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"KeywordName({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, KeywordName):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        def validate(value: str) -> KeywordName:
            return cls(value)

        from_str = core_schema.chain_schema(
            [
                core_schema.str_schema(),
                core_schema.no_info_plain_validator_function(validate),
            ]
        )

        return core_schema.json_or_python_schema(
            json_schema=from_str,
            python_schema=core_schema.union_schema(
                [
                    core_schema.is_instance_schema(cls),
                    from_str,
                ]
            ),
            serialization=core_schema.to_string_ser_schema(when_used="always"),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: core_schema.CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        return handler(core_schema.str_schema())
