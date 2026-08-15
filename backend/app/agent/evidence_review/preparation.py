"""LLMに渡す前の候補投影と控え。

Reviewerへ見せる列と、番号から元候補を引く控えを同じ採番で保持する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.agent.evidence_collection.contract import CollectedTask
from app.agent.evidence_collection.external_search.contract import (
    CANDIDATE_SNIPPET_MAX_CHARS,
    ExternalSearchCandidate,
)
from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
)

__all__ = [
    "EvidenceCandidateProjection",
    "EvidenceReviewInput",
    "EvidenceReviewPreparation",
    "EvidenceReviewTaskGroup",
    "ReviewCandidateEntry",
]


@dataclass(frozen=True, slots=True)
class EvidenceCandidateProjection:
    """Reviewerへ渡す内外統合candidate projection。URLを含まない。"""

    index: int
    title: str
    source_name: str | None
    published_at: datetime | None
    snippet: str | None


@dataclass(frozen=True, slots=True)
class EvidenceReviewTaskGroup:
    """Reviewerへ渡す、1 task分のgoalとcandidate projection。"""

    task_index: int
    research_goal: str
    candidates: tuple[EvidenceCandidateProjection, ...]


@dataclass(frozen=True, slots=True)
class EvidenceReviewInput:
    """Evidence Reviewer AgentのRun単位1 attempt入力。"""

    task_groups: tuple[EvidenceReviewTaskGroup, ...]
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ReviewCandidateEntry:
    """通し番号1つに対応する、Reviewerへ見せる前の元候補。"""

    index: int
    task_index: int
    source: InternalArticleSearchHit | ExternalSearchCandidate


@dataclass(frozen=True, slots=True)
class EvidenceReviewPreparation:
    """Reviewerへの候補投影と、番号から元候補を引く控えを同じ採番で保持する。"""

    task_groups: tuple[EvidenceReviewTaskGroup, ...]
    _candidate_entries: tuple[ReviewCandidateEntry, ...]

    @classmethod
    def from_tasks(cls, tasks: list[CollectedTask]) -> EvidenceReviewPreparation:
        """task順・グループ内は内部先で、投影と控えを同時に採番する。"""
        ordered_tasks = sorted(tasks, key=lambda task: task.task_index)
        review_tasks: list[EvidenceReviewTaskGroup] = []
        entries: list[ReviewCandidateEntry] = []
        for task in ordered_tasks:
            candidates: list[EvidenceCandidateProjection] = []
            for hit in task.internal_hits:
                index = len(entries)
                candidates.append(
                    EvidenceCandidateProjection(
                        index=index,
                        title=hit.content.title,
                        source_name=None,
                        published_at=hit.content.published_at,
                        snippet=_internal_candidate_snippet(hit),
                    )
                )
                entries.append(
                    ReviewCandidateEntry(
                        index=index,
                        task_index=task.task_index,
                        source=hit,
                    )
                )
            for candidate in task.external_candidates:
                index = len(entries)
                candidates.append(
                    EvidenceCandidateProjection(
                        index=index,
                        title=candidate.title,
                        source_name=candidate.source_name,
                        published_at=candidate.published_at,
                        snippet=candidate.snippet,
                    )
                )
                entries.append(
                    ReviewCandidateEntry(
                        index=index,
                        task_index=task.task_index,
                        source=candidate,
                    )
                )
            review_tasks.append(
                EvidenceReviewTaskGroup(
                    task_index=task.task_index,
                    research_goal=task.research_goal,
                    candidates=tuple(candidates),
                )
            )
        return cls(
            task_groups=tuple(review_tasks),
            _candidate_entries=tuple(entries),
        )

    def resolve_candidate(self, candidate_index: int) -> ReviewCandidateEntry | None:
        """Reviewerへ見せた番号だけを元候補へ解決する。"""
        if 0 <= candidate_index < len(self._candidate_entries):
            return self._candidate_entries[candidate_index]
        return None


def _internal_candidate_snippet(hit: InternalArticleSearchHit) -> str:
    content = hit.content
    if not content.key_points:
        combined = content.summary
    else:
        key_points = "\n".join(f"- {point}" for point in content.key_points)
        combined = f"{content.summary}\n{key_points}"
    return combined[:CANDIDATE_SNIPPET_MAX_CHARS]
