"""Direct answer の表示用増分 filter 契約。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

import pytest

from app.agent.answering.direct_answer.stream_filter import (
    DirectAnswerVisibleTextFilter,
)
from app.agent.answering.visible_text import AnswerVisibleTextFilter
from tests.agent.citation_marker_corpus import (
    BACKEND_ONLY_CASES,
    CITATION_MARKER_CASES,
    CitationMarkerCase,
)

_ALL_CORPUS_CASES = CITATION_MARKER_CASES + BACKEND_ONLY_CASES


def _new_filter() -> DirectAnswerVisibleTextFilter:
    return DirectAnswerVisibleTextFilter()


def _new_shared_filter() -> AnswerVisibleTextFilter:
    return AnswerVisibleTextFilter()


def _visible_fragments(chunks: Iterable[str]) -> list[str]:
    stream_filter = _new_filter()
    fragments: list[str] = []
    for chunk in chunks:
        fragment = stream_filter.append(chunk)
        if fragment:
            fragments.append(fragment)
    tail = stream_filter.finish()
    if tail:
        fragments.append(tail)
    return fragments


def _visible_text(chunks: Iterable[str]) -> str:
    return "".join(_visible_fragments(chunks))


def _shared_visible_text(chunks: Iterable[str]) -> str:
    stream_filter = _new_shared_filter()
    fragments = [stream_filter.append(chunk) for chunk in chunks]
    fragments.append(stream_filter.finish())
    return "".join(fragment for fragment in fragments if fragment)


def _all_two_chunk_splits(text: str) -> Iterator[list[str]]:
    for split_at in range(1, len(text)):
        yield [text[:split_at], text[split_at:]]


def _all_chunk_partitions(text: str) -> Iterator[list[str]]:
    if not text:
        yield []
        return

    boundary_count = len(text) - 1
    for split_mask in range(1 << boundary_count):
        chunks: list[str] = []
        chunk_start = 0
        for boundary in range(boundary_count):
            if split_mask & (1 << boundary):
                chunks.append(text[chunk_start : boundary + 1])
                chunk_start = boundary + 1
        chunks.append(text[chunk_start:])
        yield chunks


def test_removes_citation_marker_at_every_two_chunk_boundary() -> None:
    raw = "前[[1]]後"

    for chunks in _all_two_chunk_splits(raw):
        assert _visible_text(chunks) == "前後", chunks

    assert _visible_text(list(raw)) == "前後"


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (["先[[1]][[22]]後"], "先後"),
        (["先[[1]]", "[[22]]後"], "先後"),
        (["A[[[1]]B"], "A[B"),
        (["A[[", "[1", "]]B"], "A[B"),
        (["A[[1]]][[2]]B"], "A]B"),
        (["[[1]][[2]]"], ""),
        (["[[[[1]]]]"], "[[]]"),
    ],
)
def test_multiple_adjacent_and_overlapping_marker_prefixes_match_final_contract(
    chunks: list[str],
    expected: str,
) -> None:
    assert _visible_text(chunks) == expected


def test_removes_marker_with_more_than_sixteen_ascii_digits() -> None:
    digits = "1234567890123456789012345678901234567890"

    assert _visible_text(["前[[", digits[:19], digits[19:], "]]後"]) == "前後"


@pytest.mark.parametrize(
    "literal",
    [
        "[x]",
        "[[x]]",
        "[[１２]]",
        "[[12]",
        "[",
        "[[",
        "[[12",
        "[[12]",
    ],
)
def test_malformed_or_incomplete_marker_candidate_remains_literal(
    literal: str,
) -> None:
    raw = f"前{literal}後"

    for chunks in _all_two_chunk_splits(raw):
        assert _visible_text(chunks) == raw, chunks


def test_strips_outer_unicode_whitespace_and_preserves_internal_text() -> None:
    chunks = ["\u2003\t", "日本", "語\n", " \u00a0", "本文", "\r\n\u2003"]

    assert _visible_text(chunks) == "日本語\n \u00a0本文"


@pytest.mark.parametrize(
    "chunks",
    [
        [" ", "\t", "本", "文", " ", "\n"],
        [" \t本文", " \n"],
        [" \t", "本文 \n"],
        [" \t本", "文 \n"],
    ],
)
def test_whitespace_split_across_chunks_has_final_strip_semantics(
    chunks: list[str],
) -> None:
    assert _visible_text(chunks) == "本文"


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [" \t\r\n\u2003"],
        ["[[1]]", " \n", "[[22]]", "\u00a0"],
        ["[", "[", "12345678901234567890", "]", "]", "\n"],
    ],
)
def test_marker_and_whitespace_only_input_emits_no_visible_fragment(
    chunks: list[str],
) -> None:
    assert _visible_fragments(chunks) == []


def test_representative_input_is_independent_of_every_chunk_partition() -> None:
    raw = " \tA[[1]] B\n"

    for chunks in _all_chunk_partitions(raw):
        assert _visible_text(chunks) == "A B", chunks


def test_shared_visible_filter_matches_direct_contract_across_chunk_splits() -> None:
    raw = "[[1]] 本文"

    for chunks in _all_two_chunk_splits(raw):
        assert _shared_visible_text(chunks) == "本文", chunks

    assert _shared_visible_text(list(raw)) == "本文"
    assert _shared_visible_text(["前[[", "22]]中[[x]]後 \n"]) == "前中[[x]]後"


@pytest.mark.parametrize(
    "case",
    _ALL_CORPUS_CASES,
    ids=[case.label for case in _ALL_CORPUS_CASES],
)
def test_shared_filter_visible_text_matches_corpus_for_bulk_input(
    case: CitationMarkerCase,
) -> None:
    assert _shared_visible_text([case.text]) == case.stripped


@pytest.mark.parametrize(
    "case",
    _ALL_CORPUS_CASES,
    ids=[case.label for case in _ALL_CORPUS_CASES],
)
def test_shared_filter_visible_text_matches_corpus_char_by_char(
    case: CitationMarkerCase,
) -> None:
    assert _shared_visible_text(list(case.text)) == case.stripped


@pytest.mark.parametrize(
    "case",
    _ALL_CORPUS_CASES,
    ids=[case.label for case in _ALL_CORPUS_CASES],
)
def test_shared_filter_visible_text_matches_corpus_across_two_chunk_splits(
    case: CitationMarkerCase,
) -> None:
    for chunks in _all_two_chunk_splits(case.text):
        assert _shared_visible_text(chunks) == case.stripped, chunks


def test_shared_filter_removes_group_marker_across_every_chunk_partition() -> None:
    case = next(c for c in CITATION_MARKER_CASES if c.label == "group")

    for chunks in _all_chunk_partitions(case.text):
        assert _shared_visible_text(chunks) == case.stripped, chunks


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (["前[[1],"], "前[[1],"),
        (["前[[1], "], "前[[1],"),
    ],
)
def test_shared_filter_finish_emits_unterminated_group_candidate_as_literal(
    chunks: list[str],
    expected: str,
) -> None:
    assert _shared_visible_text(chunks) == expected


@pytest.mark.parametrize(
    "label",
    ["failed-group-overlapping-single", "failed-group-overlapping-group"],
)
def test_shared_filter_recovers_overlapping_marker_after_failed_group(
    label: str,
) -> None:
    case = next(c for c in CITATION_MARKER_CASES if c.label == label)

    for chunks in _all_chunk_partitions(case.text):
        assert _shared_visible_text(chunks) == case.stripped, chunks
