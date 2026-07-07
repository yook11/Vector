"""Stage 5 embedding を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.shared.text import normalize_mention_surface

__all__ = [
    "EmbeddingPreconditionProtocol",
    "EmbeddingReadyBuildBlockedCode",
    "EmbeddingReadyBuildBlockedError",
    "EmbeddingReadyBuildFacts",
    "ReadyForEmbedding",
]

_MAX_MENTIONS_FOR_EMBEDDING = 30


class EmbeddingReadyBuildBlockedCode(StrEnum):
    """Stage 5 Ready 構築 blocked の監査 outcome_code。"""

    ANALYZED_ARTICLE_MISSING = "embedding_ready_build_blocked_analyzed_article_missing"
    ALREADY_EMBEDDED = "embedding_ready_build_blocked_already_embedded"

    @property
    def is_idempotent_skip(self) -> bool:
        """別 worker が先に処理済みで no-op になった冪等 skip か (勝者の行と冗長)。"""
        return self is EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED


@dataclass(frozen=True, slots=True)
class EmbeddingReadyBuildFacts:
    """Stage 5 Ready 構築に必要な DB 射影。"""

    analyzable_article_id: int
    has_embedding: bool
    summary: str
    key_points: Any


class EmbeddingReadyBuildBlockedError(Exception):
    """Stage 5 入力として採用できなかった場合に投げる例外。"""

    def __init__(self, code: EmbeddingReadyBuildBlockedCode) -> None:
        self.code = code
        super().__init__(code.value)


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
    ) -> tuple[ReadyForEmbedding, int]:
        """DB 事実から Ready を構築し、監査主語の analyzable_article_id を確定する。

        対象外なら blocked 例外を投げる。analyzable_article_id は trigger 由来の
        ``analyzable_hint`` を優先し、旧 in-flight message (None) のときだけ DB 射影に
        fallback する。Ready 構築が成功した時点で facts は非 None なので、返す
        analyzable_article_id は必ず int になる。
        """
        facts = await embedding_repo.load_ready_build_facts(analyzed_article_id)
        if facts is None:
            raise EmbeddingReadyBuildBlockedError(
                EmbeddingReadyBuildBlockedCode.ANALYZED_ARTICLE_MISSING
            )

        if facts.has_embedding:
            raise EmbeddingReadyBuildBlockedError(
                EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED
            )

        ready = cls(
            analyzed_article_id=analyzed_article_id,
            text_for_embedding=_render_embedding_text(
                summary=facts.summary,
                key_points=facts.key_points,
            ),
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
