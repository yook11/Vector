"""Value objects for the NewsSource entity.

SourceName: Display name for a news source (e.g. "TechCrunch", "Hacker News").
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

_NAME_PATTERN = re.compile(r"^(?=.*\w)[\w \-\.]+$", re.UNICODE)
_NAME_MAX_LENGTH = 50


class SourceName(RootModel[str]):
    """Display name for a news source.

    Invariants:
    - Contains at least one word character
    - Only word chars (Unicode), spaces, hyphens, dots
    - 1-50 characters after trimming
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
            msg = "SourceName must not be empty"
            raise ValueError(msg)
        if len(v) > _NAME_MAX_LENGTH:
            msg = f"SourceName must be at most {_NAME_MAX_LENGTH} chars, got {len(v)}"
            raise ValueError(msg)
        if not _NAME_PATTERN.fullmatch(v):
            msg = (
                "Source name can only contain letters, numbers, spaces, "
                f"hyphens, dots, and underscores. Got: {v!r}"
            )
            raise ValueError(msg)
        return v

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"SourceName({self.root!r})"
