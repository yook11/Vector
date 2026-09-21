"""VectorDomainErrorに残る固定文字列表現の契約。"""

from __future__ import annotations

from typing import ClassVar

from app.logfire.exceptions import VectorDomainError


class _NoAttrs(VectorDomainError):
    pass


class _WithAttrs(VectorDomainError):
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("alpha", "beta")

    def __init__(self, *, alpha: str, beta: int) -> None:
        super().__init__()
        self.alpha = alpha
        self.beta = beta


def test_str_returns_class_name_only_when_safe_attrs_empty() -> None:
    """``SAFE_ATTRS = ()`` の base path で class name のみ返る (PII 不在の最小形)。"""
    assert str(_NoAttrs()) == "_NoAttrs"


def test_str_formats_safe_attrs_with_repr() -> None:
    """SAFE_ATTRS あり case は ``class(attr=value, ...)`` 固定形式。"""
    out = str(_WithAttrs(alpha="hello", beta=42))
    assert out == "_WithAttrs(alpha='hello', beta=42)"


def test_str_uses_none_for_missing_attribute() -> None:
    """SAFE_ATTRS の一部が instance に無い場合は ``None`` で埋める。"""

    class _PartialAttrs(VectorDomainError):
        SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("present", "absent")

        def __init__(self) -> None:
            super().__init__()
            self.present = "value"

    assert str(_PartialAttrs()) == "_PartialAttrs(present='value', absent=None)"
