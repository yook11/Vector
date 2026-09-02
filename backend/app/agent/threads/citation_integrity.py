"""保存する回答の引用マーカーと出典の整合性。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

import structlog

from app.agent.citation_markers import parse_citation_refs

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CitationIntegrityReport:
    marker_without_source_refs: tuple[str, ...]
    source_without_marker_refs: tuple[str, ...]

    @property
    def has_mismatch(self) -> bool:
        return bool(self.marker_without_source_refs or self.source_without_marker_refs)


def assess_citation_integrity(
    *,
    answer: str,
    source_refs: Iterable[str],
) -> CitationIntegrityReport:
    marker_refs = parse_citation_refs(answer)
    source_ref_values = _ordered_unique(source_refs)
    source_ref_set = set(source_ref_values)
    marker_ref_set = set(marker_refs)

    return CitationIntegrityReport(
        marker_without_source_refs=tuple(
            ref for ref in marker_refs if ref not in source_ref_set
        ),
        source_without_marker_refs=tuple(
            ref for ref in source_ref_values if ref not in marker_ref_set
        ),
    )


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return tuple(result)


def warn_on_citation_source_mismatch(
    *,
    run_id: UUID,
    answer: str,
    source_refs: list[str],
) -> None:
    report = assess_citation_integrity(answer=answer, source_refs=source_refs)
    if not report.has_mismatch:
        return
    logger.warning(
        "agent_citation_source_mismatch",
        run_id=str(run_id),
        marker_without_source_refs=list(report.marker_without_source_refs),
        source_without_marker_refs=list(report.source_without_marker_refs),
    )
