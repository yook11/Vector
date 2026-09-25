"""Stage 3 curation を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.collection.domain.article_limits import ARTICLE_BODY_MAX_LENGTH

__all__ = [
    "CurationReadyBuildRejectionReason",
    "CurationReadyBuildRejected",
    "CurationReadyBuildFacts",
    "ReadyForCuration",
]


class CurationReadyBuildRejectionReason(StrEnum):
    """Ready拒否の監査コードを保存済みの文字列と対応付ける。"""

    ARTICLE_MISSING = "curation_ready_build_blocked_article_missing"
    ALREADY_CURATED = "curation_ready_build_blocked_already_curated"
    ALREADY_REJECTED_AS_NOISE = "curation_ready_build_blocked_already_rejected_as_noise"
    CONTENT_TOO_LARGE = "curation_ready_build_blocked_content_too_large"
    INPUT_INVALID = "curation_ready_build_blocked_input_invalid"

    @property
    def is_idempotent_skip(self) -> bool:
        """別 worker が先に処理済みで no-op になった冪等 skip か (勝者の行と冗長)。"""
        return self in {
            CurationReadyBuildRejectionReason.ALREADY_CURATED,
            CurationReadyBuildRejectionReason.ALREADY_REJECTED_AS_NOISE,
        }


@dataclass(frozen=True, slots=True)
class CurationReadyBuildFacts:
    """Stage 3 Ready 構築に必要な DB 射影。"""

    analyzable_article_id: int
    original_title: str
    original_content: str
    has_signal_curation: bool
    has_noise_curation: bool


@dataclass(frozen=True, slots=True)
class CurationReadyBuildRejected:
    """Ready構築の拒否理由と、DBで確認した記事情報を表す。"""

    reason: CurationReadyBuildRejectionReason
    analyzable_article_id: int | None = None
    content_length: int | None = None
    max_content_length: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, CurationReadyBuildRejectionReason):
            raise TypeError("reason must be CurationReadyBuildRejectionReason")


class ReadyForCuration(BaseModel):
    """curator 入力と Stage 3 precondition を満たした不変オブジェクト。"""

    model_config = ConfigDict(frozen=True)

    analyzable_article_id: int = Field(gt=0)
    original_title: str = Field(min_length=1)
    original_content: str = Field(min_length=1, max_length=ARTICLE_BODY_MAX_LENGTH)

    @classmethod
    def from_facts(
        cls, facts: CurationReadyBuildFacts | None
    ) -> ReadyForCuration | CurationReadyBuildRejected:
        """取得済みの事実から、開始条件とモデルの入力制約を検証する。"""
        if facts is None:
            return CurationReadyBuildRejected(
                CurationReadyBuildRejectionReason.ARTICLE_MISSING
            )

        if facts.has_signal_curation:
            return CurationReadyBuildRejected(
                CurationReadyBuildRejectionReason.ALREADY_CURATED,
                analyzable_article_id=facts.analyzable_article_id,
            )

        if facts.has_noise_curation:
            return CurationReadyBuildRejected(
                CurationReadyBuildRejectionReason.ALREADY_REJECTED_AS_NOISE,
                analyzable_article_id=facts.analyzable_article_id,
            )

        try:
            return cls(
                analyzable_article_id=facts.analyzable_article_id,
                original_title=facts.original_title,
                original_content=facts.original_content,
            )
        except ValidationError as exc:
            if any(
                error["loc"] == ("original_content",)
                and error["type"] == "string_too_long"
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ):
                return CurationReadyBuildRejected(
                    CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE,
                    analyzable_article_id=facts.analyzable_article_id,
                    content_length=len(facts.original_content),
                    max_content_length=ARTICLE_BODY_MAX_LENGTH,
                )
            return CurationReadyBuildRejected(
                CurationReadyBuildRejectionReason.INPUT_INVALID,
                analyzable_article_id=facts.analyzable_article_id,
            )
