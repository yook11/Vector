"""Stage 3 curation を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CurationPreconditionProtocol",
    "CurationReadyBuildBlockedCode",
    "CurationReadyBuildBlockedError",
    "CurationReadyBuildFacts",
    "ReadyForCuration",
]


class CurationReadyBuildBlockedCode(StrEnum):
    """Stage 3 Ready 構築 blocked の監査 outcome_code。"""

    ARTICLE_MISSING = "curation_ready_build_blocked_article_missing"
    ALREADY_CURATED = "curation_ready_build_blocked_already_curated"
    ALREADY_REJECTED_AS_NOISE = "curation_ready_build_blocked_already_rejected_as_noise"
    CONTENT_TOO_LARGE = "curation_ready_build_blocked_content_too_large"


@dataclass(frozen=True, slots=True)
class CurationReadyBuildFacts:
    """Stage 3 Ready 構築に必要な DB 射影。"""

    article_id: int
    original_title: str
    original_content: str
    source_name: str | None
    has_signal_curation: bool
    has_noise_curation: bool


class CurationReadyBuildBlockedError(Exception):
    """Stage 3 入力として採用できなかった場合に投げる例外。"""

    def __init__(
        self,
        code: CurationReadyBuildBlockedCode,
        *,
        content_length: int | None = None,
        max_content_length: int | None = None,
    ) -> None:
        self.code = code
        self.content_length = content_length
        self.max_content_length = max_content_length
        super().__init__(code.value)


class CurationPreconditionProtocol(Protocol):
    """Ready 構築に必要な DB 事実だけを読む repository contract。

    構築可否と blocked 理由は ``ReadyForCuration`` が判定する。
    """

    async def load_ready_build_facts(
        self, article_id: int
    ) -> CurationReadyBuildFacts | None: ...


class ReadyForCuration(BaseModel):
    """curator 入力と Stage 3 precondition を満たした不変オブジェクト。"""

    model_config = ConfigDict(frozen=True)

    MAX_CONTENT_LENGTH: ClassVar[int] = 200_000

    article_id: int = Field(gt=0)
    original_title: str = Field(min_length=1)
    original_content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)

    @classmethod
    async def try_advance_from(
        cls,
        *,
        article_id: int,
        repo: CurationPreconditionProtocol,
    ) -> ReadyForCuration:
        """DB 事実から Ready を構築し、対象外なら blocked 例外を投げる。"""
        facts = await repo.load_ready_build_facts(article_id)
        if facts is None:
            raise CurationReadyBuildBlockedError(
                CurationReadyBuildBlockedCode.ARTICLE_MISSING
            )

        if facts.has_signal_curation:
            raise CurationReadyBuildBlockedError(
                CurationReadyBuildBlockedCode.ALREADY_CURATED
            )

        if facts.has_noise_curation:
            raise CurationReadyBuildBlockedError(
                CurationReadyBuildBlockedCode.ALREADY_REJECTED_AS_NOISE
            )

        content_length = len(facts.original_content)
        if content_length > cls.MAX_CONTENT_LENGTH:
            raise CurationReadyBuildBlockedError(
                CurationReadyBuildBlockedCode.CONTENT_TOO_LARGE,
                content_length=content_length,
                max_content_length=cls.MAX_CONTENT_LENGTH,
            )

        return cls(
            article_id=facts.article_id,
            original_title=facts.original_title,
            original_content=facts.original_content,
        )
