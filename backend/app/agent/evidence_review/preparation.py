"""LLMに渡す前の選択肢投影と、番号から元の検索結果を引く控え。同じ採番で保持する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.agent.evidence_collection.contract import CollectedTask
from app.agent.evidence_collection.external_search.contract import (
    OPTION_SNIPPET_MAX_CHARS,
    ExternalSearchHit,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)

__all__ = [
    "EvidenceOption",
    "EvidenceOptionOrigin",
    "EvidenceReviewInput",
    "EvidenceReviewPreparation",
    "EvidenceReviewTaskGroup",
]


@dataclass(frozen=True, slots=True)
class EvidenceOption:
    """Reviewerへ見せる1件。内部記事と外部検索の記事を区別せず、URLを持たない。"""

    index: int
    title: str
    source_name: str | None
    published_at: datetime | None
    snippet: str | None


@dataclass(frozen=True, slots=True)
class EvidenceReviewTaskGroup:
    """1 task分のgoalと、そのtaskに属する選択肢。"""

    task_index: int
    research_goal: str
    options: tuple[EvidenceOption, ...]


@dataclass(frozen=True, slots=True)
class EvidenceReviewInput:
    """Reviewer 1 attemptの入力。"""

    task_groups: tuple[EvidenceReviewTaskGroup, ...]
    as_of: datetime


@dataclass(frozen=True, slots=True)
class EvidenceOptionOrigin:
    """レビュアーに見せた番号に対応する元の検索結果。URLを含むため復元専用。"""

    index: int
    task_index: int
    search_hit: InternalArticleSearchHit | ExternalSearchHit


@dataclass(frozen=True, slots=True)
class EvidenceReviewPreparation:
    """見せた列と控えを同じ採番で持つ。"""

    task_groups: tuple[EvidenceReviewTaskGroup, ...]
    _option_origins: tuple[EvidenceOptionOrigin, ...]

    @classmethod
    def from_tasks(cls, tasks: list[CollectedTask]) -> EvidenceReviewPreparation:
        """task_index順、グループ内は内部記事を先に、選択肢と元の検索結果を同時に採番する。"""
        ordered_tasks = sorted(tasks, key=lambda task: task.task_index)
        review_tasks: list[EvidenceReviewTaskGroup] = []
        origins: list[EvidenceOptionOrigin] = []
        for task in ordered_tasks:
            options: list[EvidenceOption] = []
            for hit in task.internal_hits:
                index = len(origins)
                options.append(
                    EvidenceOption(
                        index=index,
                        title=hit.content.title,
                        source_name=None,
                        published_at=hit.content.published_at,
                        snippet=_internal_option_snippet(hit),
                    )
                )
                origins.append(
                    EvidenceOptionOrigin(
                        index=index,
                        task_index=task.task_index,
                        search_hit=hit,
                    )
                )
            for hit in task.external_hits:
                index = len(origins)
                options.append(
                    EvidenceOption(
                        index=index,
                        title=hit.title,
                        source_name=hit.source_name,
                        published_at=hit.published_at,
                        snippet=hit.snippet,
                    )
                )
                origins.append(
                    EvidenceOptionOrigin(
                        index=index,
                        task_index=task.task_index,
                        search_hit=hit,
                    )
                )
            review_tasks.append(
                EvidenceReviewTaskGroup(
                    task_index=task.task_index,
                    research_goal=task.research_goal,
                    options=tuple(options),
                )
            )
        return cls(
            task_groups=tuple(review_tasks),
            _option_origins=tuple(origins),
        )

    def resolve_option_origin(self, option_index: int) -> EvidenceOptionOrigin | None:
        """見せた番号だけを元の検索結果へ解決する。"""
        if 0 <= option_index < len(self._option_origins):
            return self._option_origins[option_index]
        return None


def _internal_option_snippet(hit: InternalArticleSearchHit) -> str:
    content = hit.content
    if not content.key_points:
        combined = content.summary
    else:
        key_points = "\n".join(f"- {point}" for point in content.key_points)
        combined = f"{content.summary}\n{key_points}"
    return combined[:OPTION_SNIPPET_MAX_CHARS]
