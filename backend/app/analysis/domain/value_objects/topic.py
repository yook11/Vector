"""Topic の値オブジェクト。

TopicName: AI が記事ごとに生成する分類ラベル。正規化済みの英語小文字。
例: "ai agents", "quantum computing", "6g"

正規化ルール（v2）:
- ハイフン／アンダースコアは語境界として扱い、単一空白に畳む
- 最大 3 語（空白区切り）
- 冠詞・前置詞（a / an / the / in / of）は含めない
これにより "ai agents" と "ai-agents" のような表記揺れが構造的に吸収される。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

_TOPIC_PATTERN = re.compile(r"^[a-z0-9]+( [a-z0-9]+)*$")
_TOPIC_MAX_LENGTH = 100
_TOPIC_MIN_LENGTH = 2
_TOPIC_MAX_WORDS = 3
_STOPWORDS: frozenset[str] = frozenset({"a", "an", "the", "in", "of"})


class TopicName(RootModel[str]):
    """AI 生成の分類ラベル名。正規化済みの英語小文字。

    入力は自動で正規化される（小文字化、ハイフン／アンダースコアを空白に、
    連続空白を単一空白に）。VO 自身が「正規化済み」を保証する状態型。

    Invariants:
    - 英数字トークンを単一空白で連結した形式
    - 先頭・末尾は英数字
    - 2-100 文字
    - 小文字のみ
    - 最大 3 語
    - 冠詞・前置詞を含まない
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
        s = re.sub(r"[-_]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()

        if len(s) < _TOPIC_MIN_LENGTH:
            msg = f"TopicName must be at least {_TOPIC_MIN_LENGTH} chars, got {len(s)}"
            raise ValueError(msg)
        if len(s) > _TOPIC_MAX_LENGTH:
            msg = f"TopicName must be at most {_TOPIC_MAX_LENGTH} chars, got {len(s)}"
            raise ValueError(msg)
        if not _TOPIC_PATTERN.fullmatch(s):
            msg = (
                "TopicName must contain only lowercase letters, numbers, "
                f"and single spaces. Got: {v!r}"
            )
            raise ValueError(msg)

        tokens = s.split(" ")
        if len(tokens) > _TOPIC_MAX_WORDS:
            msg = (
                f"TopicName must be at most {_TOPIC_MAX_WORDS} words "
                f"(got {len(tokens)}: {v!r})"
            )
            raise ValueError(msg)
        banned = _STOPWORDS.intersection(tokens)
        if banned:
            msg = (
                f"TopicName must not contain article/preposition stopwords "
                f"{sorted(banned)} (got {v!r})"
            )
            raise ValueError(msg)

        return s

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"TopicName({self.root!r})"
