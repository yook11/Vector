"""Logfire SaaS への PII 流出を防ぐ domain exception 基底。

``__str__`` を class name + ``SAFE_ATTRS`` の固定形式に縛り、URL / 本文 /
prompt / AI response などが span の exception message に混ざる経路を塞ぐ。
"""

from __future__ import annotations

from typing import ClassVar


class VectorDomainError(Exception):
    """Vector domain exception の基底。``__str__`` は class name + SAFE_ATTRS のみ。

    subclass は ``SAFE_ATTRS: ClassVar[tuple[str, ...]]`` で「``__str__``
    で公開して安全な field 名」を宣言する。PII を含む field は列挙しない。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ()
    """``__str__`` で公開して安全な field 名 (PII を含まない)。subclass が宣言する。"""

    def __str__(self) -> str:
        attrs = {name: getattr(self, name, None) for name in self.SAFE_ATTRS}
        if not attrs:
            return self.__class__.__name__
        attr_str = ", ".join(f"{k}={v!r}" for k, v in attrs.items())
        return f"{self.__class__.__name__}({attr_str})"
