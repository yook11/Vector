"""Value object for validated HTTP/HTTPS URLs.

SafeUrl ensures a URL uses a safe scheme (http or https) and has a valid
structure. Validation delegates to Pydantic's AnyHttpUrl, but the stored
value is the original string (after strip) — no normalization is applied.
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
    """HTTP/HTTPS URL validated by Pydantic.

    Invariants:
    - Uses http or https scheme
    - Valid URL structure (scheme + host at minimum)
    - 1-2048 characters after trimming
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
