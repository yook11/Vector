"""citation marker 構文 (正規表現実装) のテスト。

`backend/specs/agent-citation-marker-grouped-refs-slice.md` の「受理する文法」
「内側カンマ形は受理しない」「構文の SSoT を 1 箇所に置く」に対応する。
"""

from __future__ import annotations

import pytest

from app.agent.citation_markers import parse_citation_refs, strip_citation_markers
from tests.agent.citation_marker_corpus import (
    BACKEND_ONLY_CASES,
    CITATION_MARKER_CASES,
    CitationMarkerCase,
)

_ALL_CASES = CITATION_MARKER_CASES + BACKEND_ONLY_CASES


@pytest.mark.parametrize(
    "case",
    _ALL_CASES,
    ids=[case.label for case in _ALL_CASES],
)
def test_parse_citation_refs_matches_corpus(case: CitationMarkerCase) -> None:
    assert parse_citation_refs(case.text) == case.refs


@pytest.mark.parametrize(
    "case",
    _ALL_CASES,
    ids=[case.label for case in _ALL_CASES],
)
def test_strip_citation_markers_matches_corpus(case: CitationMarkerCase) -> None:
    assert strip_citation_markers(case.text) == case.stripped


@pytest.mark.parametrize(
    "case",
    _ALL_CASES,
    ids=[case.label for case in _ALL_CASES],
)
def test_corpus_text_starts_and_ends_with_non_space(case: CitationMarkerCase) -> None:
    """コーパスの docstring が明示する前提条件 (外側に空白を持たない) を固定する。"""

    assert not case.text[0].isspace()
    assert not case.text[-1].isspace()


@pytest.mark.parametrize(
    "case",
    [case for case in _ALL_CASES if case.refs],
    ids=[case.label for case in _ALL_CASES if case.refs],
)
def test_corpus_cases_with_refs_are_actually_stripped(case: CitationMarkerCase) -> None:
    """refs が非空なら本文が変化することを固定し、コーパスの空虚化を防ぐ。"""

    assert case.stripped != case.text


def test_parse_citation_refs_returns_empty_tuple_for_empty_string() -> None:
    assert parse_citation_refs("") == ()


def test_parse_citation_refs_returns_empty_tuple_when_no_marker_present() -> None:
    assert parse_citation_refs("marker を含まない本文です") == ()


def test_strip_citation_markers_returns_input_unchanged_for_empty_string() -> None:
    assert strip_citation_markers("") == ""


def test_strip_citation_markers_returns_input_unchanged_when_no_marker_present() -> (
    None
):
    text = "marker を含まない本文です"

    assert strip_citation_markers(text) == text
