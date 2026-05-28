"""Stage 3 curation を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CurationPreconditionProtocol",
    "CurationReadyBuildBlocked",
    "CurationReadyBuildBlockedCode",
    "CurationReadyBuildBlockedError",
    "CurationReadyBuildFacts",
    "ReadyForCuration",
]


class CurationReadyBuildBlockedCode(StrEnum):
    """Stage 3 Ready 構築が業務状態により進めなかった理由。"""

    ARTICLE_MISSING = "article_missing"
    ALREADY_CURATED = "already_curated"
    ALREADY_REJECTED_AS_NOISE = "already_rejected_as_noise"
    CONTENT_TOO_LARGE = "content_too_large"


@dataclass(frozen=True, slots=True)
class CurationReadyBuildBlocked:
    """Stage 3 Ready 構築が正常に判定され、対象外だった結果。"""

    target_article_id: int
    code: CurationReadyBuildBlockedCode
    content_length: int | None = None
    max_content_length: int | None = None
    source_name: str | None = None


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
    """Stage 3 Ready 構築が業務状態により進めなかったことを表す例外。"""

    def __init__(self, blocked: CurationReadyBuildBlocked) -> None:
        self.blocked = blocked
        super().__init__(blocked.code.value)


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
                CurationReadyBuildBlocked(
                    target_article_id=article_id,
                    code=CurationReadyBuildBlockedCode.ARTICLE_MISSING,
                )
            )

        if facts.has_signal_curation:
            raise CurationReadyBuildBlockedError(
                CurationReadyBuildBlocked(
                    target_article_id=article_id,
                    code=CurationReadyBuildBlockedCode.ALREADY_CURATED,
                    source_name=facts.source_name,
                )
            )

        if facts.has_noise_curation:
            raise CurationReadyBuildBlockedError(
                CurationReadyBuildBlocked(
                    target_article_id=article_id,
                    code=CurationReadyBuildBlockedCode.ALREADY_REJECTED_AS_NOISE,
                    source_name=facts.source_name,
                )
            )

        content_length = len(facts.original_content)
        if content_length > cls.MAX_CONTENT_LENGTH:
            raise CurationReadyBuildBlockedError(
                CurationReadyBuildBlocked(
                    target_article_id=article_id,
                    code=CurationReadyBuildBlockedCode.CONTENT_TOO_LARGE,
                    content_length=content_length,
                    max_content_length=cls.MAX_CONTENT_LENGTH,
                    source_name=facts.source_name,
                )
            )

        return cls(
            article_id=facts.article_id,
            original_title=facts.original_title,
            original_content=facts.original_content,
        )
