"""Citation marker/source integrity helpers."""

from __future__ import annotations

import pytest

from app.agent.runs.citation_integrity import assess_citation_integrity


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


# --- グループ形 marker 受理 (spec: agent-citation-marker-grouped-refs-slice.md) ---
# validation.py だけがグループ形を認識しcitation_integrity.pyが認識しないと、
# cited_refsに載ったrefがsource_without_marker_refsとして偽warningを出し続ける
# (Invariants)。


def test_assess_citation_integrity_accepts_group_marker_matching_all_source_refs() -> (
    None
):
    """グループ形 [[1], [2]] は正準形と同じref集合として扱われ、
    偽warning (source_without_marker_refs) を出さない(回帰テスト)。"""
    report = assess_citation_integrity(
        answer="複数の根拠を示します。[[1], [2]]",
        source_refs=["1", "2"],
    )

    assert not report.has_mismatch
    assert report.marker_without_source_refs == ()
    assert report.source_without_marker_refs == ()


def test_assess_citation_integrity_reports_group_marker_ref_missing_from_sources() -> (
    None
):
    """グループ形のrefのうちsource_refsに無いものがmarker_without_source_refsに
    出る。"""
    report = assess_citation_integrity(
        answer="[[1], [2]] を引用します。",
        source_refs=["1"],
    )

    assert report.has_mismatch
    assert report.marker_without_source_refs == ("2",)
    assert report.source_without_marker_refs == ()
