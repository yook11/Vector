"""Value objects for the Keyword entity.

KeywordName: A tag representing a specific technology or theme within a sector.
Examples: "large language model", "AI/ML", "C++", "Node.js", "量子エラー訂正"
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

_KEYWORD_PATTERN = re.compile(r"^(?=.*\w)[\w \-\.&/+#]+$", re.UNICODE)
_KEYWORD_MAX_LENGTH = 100


class KeywordName(RootModel[str]):
    """Tag name for a technology or theme within a sector.

    Invariants:
    - Contains at least one word character (\\w)
    - Only word chars (Unicode), spaces, hyphens, dots, &, /, +, #
    - 1-100 characters after trimming
    - Immutable after creation
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
            msg = "KeywordName must not be empty"
            raise ValueError(msg)
        if len(v) > _KEYWORD_MAX_LENGTH:
            msg = (
                f"KeywordName must be at most {_KEYWORD_MAX_LENGTH} chars, got {len(v)}"
            )
            raise ValueError(msg)
        if not _KEYWORD_PATTERN.fullmatch(v):
            msg = (
                "KeywordName can only contain letters, numbers, spaces, "
                "hyphens, dots, &, /, +, #, and underscores. "
                f"Got: {v!r}"
            )
            raise ValueError(msg)
        return v

    @property
    def value(self) -> str:
        """Backward compat — prefer .root in new code."""
        return self.root

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"KeywordName({self.root!r})"
