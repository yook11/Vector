"""Citation marker/source integrity helpers."""

from __future__ import annotations

import pytest

from app.agent.history.citation_integrity import assess_citation_integrity


@pytest.mark.parametrize(
    ("answer", "source_refs"),
    [
        ("根拠あり [[1]]。続けて [[2]]。", ["1", "2"]),
        ("連続 marker [[1]][[2]] と再利用 [[1]]。", ["1", "2"]),
        ("marker なし direct answer", []),
    ],
)
def test_assess_citation_integrity_accepts_matching_refs(
    answer: str,
    source_refs: list[str],
) -> None:
    report = assess_citation_integrity(answer=answer, source_refs=source_refs)

    assert not report.has_mismatch
    assert report.marker_without_source_refs == ()
    assert report.source_without_marker_refs == ()


def test_assess_citation_integrity_reports_both_mismatch_directions() -> None:
    report = assess_citation_integrity(
        answer="回答は [[2]] と [[4]] を引用します。[[2]] は重複します。",
        source_refs=["1", "2", "3", "3"],
    )

    assert report.has_mismatch
    assert report.marker_without_source_refs == ("4",)
    assert report.source_without_marker_refs == ("1", "3")


def test_assess_citation_integrity_reports_sources_without_markers() -> None:
    report = assess_citation_integrity(
        answer="source はあるが marker はありません。",
        source_refs=["1"],
    )

    assert report.has_mismatch
    assert report.marker_without_source_refs == ()
    assert report.source_without_marker_refs == ("1",)
