"""Topic の値オブジェクト。

TopicName: AI が記事ごとに生成する分類ラベル。正規化済みの英語小文字。
例: "ai drug discovery", "semiconductor trade policy", "6g network"
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

_TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9 -]*[a-z0-9]$")
_TOPIC_MAX_LENGTH = 100
_TOPIC_MIN_LENGTH = 2


class TopicName(RootModel[str]):
    """AI 生成の分類ラベル名。正規化済みの英語小文字。

    入力は自動で正規化される（小文字化、空白/ハイフン正規化）。
    VO 自身が「正規化済み」を保証する状態型。

    Invariants:
    - 先頭・末尾は英数字
    - 中間は英数字・スペース・ハイフンのみ
    - 2-100 文字
    - 小文字のみ
    - 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        if not isinstance(v, str):
            msg = f"Expected str, got {type(v).__name__}"
            raise ValueError(msg)
        s = v.strip().lower()
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"\s*-\s*", "-", s)
        s = re.sub(r"-+", "-", s)
        if len(s) < _TOPIC_MIN_LENGTH:
            msg = f"TopicName must be at least {_TOPIC_MIN_LENGTH} chars, got {len(s)}"
            raise ValueError(msg)
        if len(s) > _TOPIC_MAX_LENGTH:
            msg = f"TopicName must be at most {_TOPIC_MAX_LENGTH} chars, got {len(s)}"
            raise ValueError(msg)
        if not _TOPIC_PATTERN.fullmatch(s):
            msg = (
                "TopicName must contain only lowercase letters, numbers, "
                f"spaces, and hyphens. Got: {v!r}"
            )
            raise ValueError(msg)
        return s

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"TopicName({self.root!r})"
