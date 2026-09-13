"""Stage 5 embedding を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.shared.text import normalize_mention_surface

__all__ = [
    "EmbeddingPreconditionProtocol",
    "EmbeddingReadyBuildRejectionReason",
    "EmbeddingReadyBuildRejected",
    "EmbeddingReadyBuildFacts",
    "ReadyForEmbedding",
]

_MAX_MENTIONS_FOR_EMBEDDING = 30


class EmbeddingReadyBuildRejectionReason(StrEnum):
    """Ready構築を拒否する理由と既存の監査コード。"""

    ANALYZED_ARTICLE_MISSING = "embedding_ready_build_blocked_analyzed_article_missing"
    INPUT_INVALID = "embedding_ready_build_blocked_input_invalid"
    ALREADY_EMBEDDED = "embedding_ready_build_blocked_already_embedded"

    @property
    def is_idempotent_skip(self) -> bool:
        """別 worker が先に処理済みで no-op になった冪等 skip か (勝者の行と冗長)。"""
        return self is EmbeddingReadyBuildRejectionReason.ALREADY_EMBEDDED


@dataclass(frozen=True, slots=True)
class EmbeddingReadyBuildFacts:
    """Stage 5 Ready 構築に必要な DB 射影。"""

    analyzable_article_id: int
    has_embedding: bool
    summary: str
    key_points: Any


@dataclass(frozen=True, slots=True)
class EmbeddingReadyBuildRejected:
    """Readyを構築できない理由とDBで確認した記事IDを表す。"""

    reason: EmbeddingReadyBuildRejectionReason
    analyzable_article_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, EmbeddingReadyBuildRejectionReason):
            raise TypeError("reason must be EmbeddingReadyBuildRejectionReason")


class EmbeddingPreconditionProtocol(Protocol):
    """Ready 構築に必要な DB 事実だけを読む repository contract。

    構築可否と blocked 理由は ``ReadyForEmbedding`` が判定する。
    """

    async def load_ready_build_facts(
        self, analyzed_article_id: int
    ) -> EmbeddingReadyBuildFacts | None: ...


class ReadyForEmbedding(BaseModel):
    """embedder 入力と Stage 5 precondition を満たした不変オブジェクト。"""

    model_config = ConfigDict(frozen=True)

    analyzed_article_id: int = Field(gt=0)
    text_for_embedding: str = Field(min_length=1)

    @classmethod
    async def try_advance_from(
        cls,
        analyzed_article_id: int,
        embedding_repo: EmbeddingPreconditionProtocol,
        *,
        analyzable_hint: int | None = None,
    ) -> tuple[ReadyForEmbedding, int] | EmbeddingReadyBuildRejected:
        """DB 事実から Ready を構築し、監査主語の analyzable_article_id を確定する。

        構築できない場合は理由付きの拒否結果を返す。
        analyzable_article_id は trigger 由来の
        ``analyzable_hint`` を優先し、旧 in-flight message (None) のときだけ DB 射影に
        fallback する。Ready 構築が成功した時点で facts は非 None なので、返す
        analyzable_article_id は必ず int になる。
        """
        facts = await embedding_repo.load_ready_build_facts(analyzed_article_id)
        return cls.from_facts(
            analyzed_article_id, facts, analyzable_hint=analyzable_hint
        )

    @classmethod
    def from_facts(
        cls,
        analyzed_article_id: int,
        facts: EmbeddingReadyBuildFacts | None,
        *,
        analyzable_hint: int | None = None,
    ) -> tuple[ReadyForEmbedding, int] | EmbeddingReadyBuildRejected:
        """取得済みの事実から、I/Oなしで開始条件と入力を検証する。"""
        if facts is None:
            return EmbeddingReadyBuildRejected(
                EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING
            )

        if facts.has_embedding:
            return EmbeddingReadyBuildRejected(
                EmbeddingReadyBuildRejectionReason.ALREADY_EMBEDDED,
                analyzable_article_id=facts.analyzable_article_id,
            )

        text_for_embedding = _render_embedding_text(
            summary=facts.summary,
            key_points=facts.key_points,
        )
        try:
            ready = cls(
                analyzed_article_id=analyzed_article_id,
                text_for_embedding=text_for_embedding,
            )
        except ValidationError:
            return EmbeddingReadyBuildRejected(
                EmbeddingReadyBuildRejectionReason.INPUT_INVALID,
                analyzable_article_id=facts.analyzable_article_id,
            )
        analyzable_article_id = (
            analyzable_hint
            if analyzable_hint is not None
            else facts.analyzable_article_id
        )
        return ready, analyzable_article_id


def _render_embedding_text(*, summary: str, key_points: Any) -> str:
    sections = [summary]
    contents = _extract_key_point_contents(key_points)
    if contents:
        sections.append("\n".join(contents))

    mentions = _extract_mention_surfaces(key_points)
    if mentions:
        sections.append(", ".join(mentions))

    return "\n\n".join(sections)


def _extract_key_point_contents(key_points: Any) -> list[str]:
    if not isinstance(key_points, list):
        return []
    contents: list[str] = []
    for key_point in key_points:
        if not isinstance(key_point, dict):
            continue
        content = key_point.get("content")
        if not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            contents.append(content)
    return contents


def _extract_mention_surfaces(key_points: Any) -> list[str]:
    if not isinstance(key_points, list):
        return []
    mentions: list[str] = []
    seen: set[str] = set()
    for key_point in key_points:
        if not isinstance(key_point, dict):
            continue
        content = key_point.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        raw_mentions = key_point.get("mentions")
        if not isinstance(raw_mentions, list):
            continue
        for mention in raw_mentions:
            if not isinstance(mention, dict):
                continue
            surface = mention.get("surface")
            if not isinstance(surface, str):
                continue
            normalized = normalize_mention_surface(surface)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            mentions.append(normalized)
            if len(mentions) >= _MAX_MENTIONS_FOR_EMBEDDING:
                return mentions
    return mentions
