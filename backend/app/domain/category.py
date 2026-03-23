"""Value objects for the Category entity.

CategorySlug: URL-safe identifier for categories (e.g. "ai_ml", "semiconductor").
CategoryName: Japanese display name for categories (e.g. "AI・ML", "半導体").
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema


class CategorySlug:
    """URL-safe category identifier.

    Invariants:
    - Starts with lowercase letter or digit
    - Contains only lowercase letters, digits, and underscores
    - 1-50 characters
    - Immutable after creation
    """

    __slots__ = ("_value",)
    _PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,49}$")

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            msg = f"Expected str, got {type(value).__name__}"
            raise TypeError(msg)
        v = value.strip()
        if not self._PATTERN.fullmatch(v):
            msg = (
                f"CategorySlug must start with a lowercase letter or digit, "
                f"contain only [a-z0-9_], and be 1-50 chars. Got: {v!r}"
            )
            raise ValueError(msg)
        object.__setattr__(self, "_value", v)

    @property
    def value(self) -> str:
        return self._value

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("CategorySlug is immutable")

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"CategorySlug({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CategorySlug):
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
        def validate(value: str) -> CategorySlug:
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


class CategoryName:
    """Japanese display name for a category.

    Invariants:
    - Contains word characters, middle dot (・), spaces, or hyphens
    - Not empty or whitespace-only
    - 1-50 characters
    - Immutable after creation
    """

    __slots__ = ("_value",)
    _PATTERN = re.compile(r"^[\w・ \-]+$", re.UNICODE)
    _MAX_LENGTH = 50

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            msg = f"Expected str, got {type(value).__name__}"
            raise TypeError(msg)
        v = value.strip()
        if not v:
            msg = "CategoryName must not be empty"
            raise ValueError(msg)
        if len(v) > self._MAX_LENGTH:
            msg = f"CategoryName must be at most {self._MAX_LENGTH} chars, got {len(v)}"
            raise ValueError(msg)
        if not self._PATTERN.fullmatch(v):
            msg = (
                f"CategoryName can only contain word characters, ・, spaces, "
                f"or hyphens. Got: {v!r}"
            )
            raise ValueError(msg)
        object.__setattr__(self, "_value", v)

    @property
    def value(self) -> str:
        return self._value

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("CategoryName is immutable")

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"CategoryName({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CategoryName):
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
        def validate(value: str) -> CategoryName:
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
