"""Validate completed-run citation markers against saved source refs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_CITATION_MARKER_RE = re.compile(r"\[\[([0-9]+)\]\]")


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
    marker_refs = _citation_refs_from_answer(answer)
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


def _citation_refs_from_answer(answer: str) -> tuple[str, ...]:
    return _ordered_unique(
        match.group(1) for match in _CITATION_MARKER_RE.finditer(answer)
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
