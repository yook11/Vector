"""Evidence JSONからanswer stringだけを増分復元する契約。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from importlib import import_module
from typing import Protocol, cast

import pytest


class _JsonAnswerExtractor(Protocol):
    def append(self, raw_fragment: str) -> str: ...

    def finish(self) -> str: ...


class _VisibleTextFilter(Protocol):
    def append(self, text: str) -> str: ...

    def finish(self) -> str: ...


def _new_extractor() -> _JsonAnswerExtractor:
    try:
        module = import_module(
            "app.agent.answering.evidence_answer.json_answer_extractor"
        )
    except ModuleNotFoundError as exc:
        if exc.name != "app.agent.answering.evidence_answer.json_answer_extractor":
            raise
        pytest.fail("Evidence JSON answer extractorが未実装です", pytrace=False)

    extractor_type = getattr(module, "IncrementalJsonAnswerExtractor", None)
    assert extractor_type is not None, "IncrementalJsonAnswerExtractor が未実装です"
    return cast("_JsonAnswerExtractor", extractor_type())


def _new_visible_filter() -> _VisibleTextFilter:
    try:
        module = import_module("app.agent.answering.visible_text")
    except ModuleNotFoundError as exc:
        if exc.name != "app.agent.answering.visible_text":
            raise
        pytest.fail("共有の回答表示filterが未実装です", pytrace=False)

    filter_type = getattr(module, "AnswerVisibleTextFilter", None)
    assert filter_type is not None, "AnswerVisibleTextFilter が未実装です"
    return cast("_VisibleTextFilter", filter_type())


def _extract(chunks: Iterable[str]) -> str:
    extractor = _new_extractor()
    fragments = [extractor.append(chunk) for chunk in chunks]
    fragments.append(extractor.finish())
    return "".join(fragment for fragment in fragments if fragment)


def _extract_visible(chunks: Iterable[str]) -> str:
    extractor = _new_extractor()
    visible_filter = _new_visible_filter()
    visible: list[str] = []
    for chunk in chunks:
        decoded = extractor.append(chunk)
        if decoded:
            fragment = visible_filter.append(decoded)
            if fragment:
                visible.append(fragment)
    decoded_tail = extractor.finish()
    if decoded_tail:
        fragment = visible_filter.append(decoded_tail)
        if fragment:
            visible.append(fragment)
    visible_tail = visible_filter.finish()
    if visible_tail:
        visible.append(visible_tail)
    return "".join(visible)


def _all_two_chunk_splits(text: str) -> Iterator[list[str]]:
    for split_at in range(len(text) + 1):
        yield [text[:split_at], text[split_at:]]


@pytest.mark.parametrize(
    ("raw_json", "expected"),
    [
        (
            '{"sufficiency":"answered","metadata":{"answer":"nested"},'
            '"cited_refs":["hidden"],"answer":"本文",'
            '"missing_aspects":["hidden aspect"]}',
            "本文",
        ),
        (
            '{"answer":"先頭","missing_aspects":[],"cited_refs":[],'
            '"sufficiency":"insufficient"}',
            "先頭",
        ),
        (
            '{"missing_aspects":[],"cited_refs":[],"answer":"末尾",'
            '"sufficiency":"insufficient"}',
            "末尾",
        ),
        ('{"metadata":{"answer":"nested only"}}', ""),
        ('[{"answer":"array nested"}]', ""),
        ('{"answer":123}', ""),
    ],
)
def test_extracts_only_root_object_answer_independent_of_field_order(
    raw_json: str,
    expected: str,
) -> None:
    assert _extract([raw_json]) == expected


def test_empty_fragments_one_character_chunks_and_every_split_are_equivalent() -> None:
    raw_json = '{"answer":"A"}'

    for chunks in _all_two_chunk_splits(raw_json):
        assert _extract(chunks) == "A", chunks

    assert _extract(list(raw_json)) == "A"
    assert _extract(["", '{"ans', "", 'wer":"', "A", "", '"}', ""]) == "A"


def test_decodes_every_json_string_escape_without_exposing_syntax() -> None:
    raw_json = (
        r'{"answer":"quote:\" slash:\/ backslash:\\ newline:\n tab:\t '
        r'backspace:\b formfeed:\f return:\r"}'
    )
    expected = (
        'quote:" slash:/ backslash:\\ newline:\n tab:\t '
        "backspace:\b formfeed:\f return:\r"
    )

    assert _extract(list(raw_json)) == expected


def test_unicode_escapes_surrogate_pair_and_raw_unicode_survive_every_split() -> None:
    escaped = r'{"answer":"\u65E5\u672C \uD83D\uDE80"}'
    raw_unicode = '{"answer":"生の日本語🚀"}'

    for chunks in _all_two_chunk_splits(escaped):
        assert _extract(chunks) == "日本 🚀", chunks
    for chunks in _all_two_chunk_splits(raw_unicode):
        assert _extract(chunks) == "生の日本語🚀", chunks


def test_high_surrogate_is_held_until_the_low_surrogate_arrives() -> None:
    extractor = _new_extractor()

    first = extractor.append(r'{"answer":"prefix \uD83D')
    second = extractor.append(r'\uDE80 suffix"}')
    tail = extractor.finish()

    assert first == "prefix "
    assert second + tail == "🚀 suffix"


@pytest.mark.parametrize(
    ("raw_json", "expected"),
    [
        (r'{"answer":"ok\u12', "ok"),
        (r'{"answer":"ok\uD83D', "ok"),
        (r'{"answer":"ok' + "\\", "ok"),
    ],
)
def test_finish_does_not_emit_incomplete_escape_or_surrogate(
    raw_json: str,
    expected: str,
) -> None:
    assert _extract([raw_json]) == expected


def test_json_delimiters_inside_answer_string_are_preserved_as_text() -> None:
    raw_json = '{"answer":"object {a:b}, array [1,2]: done"}'

    assert _extract(list(raw_json)) == "object {a:b}, array [1,2]: done"


def test_insufficient_sufficiency_does_not_suppress_answer_extraction() -> None:
    raw_json = (
        '{"missing_aspects":["一次情報"],"answer":"確認できる範囲",'
        '"sufficiency":"insufficient","cited_refs":[]}'
    )

    assert _extract(list(raw_json)) == "確認できる範囲"


def test_extracted_answer_and_shared_visible_filter_match_explicit_final_text() -> None:
    raw_json = (
        '{"sufficiency":"answered","answer":" [[1]] 本文 [[2]] ",'
        '"cited_refs":["1","2"],"missing_aspects":[]}'
    )

    for chunks in _all_two_chunk_splits(raw_json):
        assert _extract_visible(chunks) == "本文", chunks
