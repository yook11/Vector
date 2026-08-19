"""EvidenceReviewPreparation の契約(Run単位の投影と復元)。

Run内の選択肢をtask_index昇順のグループ列(indexはRun全体の通し番号)へ組み、
見せた番号から元の出所を引き戻せる往復を保証する。Reviewerへ渡す型が
出所種別やURLを含まないことも、この投影の契約に含まれる。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest

from app.agent.evidence_collection.external_search.contract import (
    EXTERNAL_CONTENT_MAX_CHARS,
)
from app.agent.evidence_review.preparation import (
    OPTION_BODY_MAX_CHARS,
    EvidenceOption,
    EvidenceReviewInput,
    EvidenceReviewPreparation,
    EvidenceReviewTaskGroup,
)
from tests.agent.evidence_review._builders import (
    AS_OF,
    collected_task,
    external_hit,
    internal_hit,
    review_input,
    sample_task,
    task_group,
)


def _assert_frozen_slots_dataclass(value: type[object]) -> None:
    assert is_dataclass(value) and value.__dataclass_params__.frozen
    assert hasattr(value, "__slots__") and "__dict__" not in value.__slots__


# --- Reviewerへ渡す型の構成 ---------------------------------------------------


def test_evidence_option_is_unified_and_excludes_source_metadata() -> None:
    """保証するテスト条件 1。内外で同一 field 構成、出所種別・URL 等を含まない。"""
    _assert_frozen_slots_dataclass(EvidenceOption)
    field_names = {field.name for field in fields(EvidenceOption)}
    assert field_names == {"index", "title", "source_name", "published_at", "body"}
    forbidden = {
        "url",
        "assessment_id",
        "curation_id",
        "source_ref",
        "kind",
        "origin",
        "is_internal",
        "option_type",
    }
    assert not (field_names & forbidden)


def test_review_input_carries_only_task_groups_and_as_of() -> None:
    """v3(判定基準の一本化)。AnswerBrief 全体(standalone_question 等)も

    content_requirementsも持たず、research_goal(task_groups経由)とas_ofだけで
    判定する入力になる。
    """
    _assert_frozen_slots_dataclass(EvidenceReviewInput)
    field_names = [field.name for field in fields(EvidenceReviewInput)]
    assert set(field_names) == {"task_groups", "as_of"}

    prepared_input = review_input()
    with pytest.raises(FrozenInstanceError):
        prepared_input.task_groups = ()
    assert not hasattr(prepared_input, "content_requirements")
    assert not hasattr(prepared_input, "standalone_question")
    assert not hasattr(prepared_input, "response_requirements")
    assert not hasattr(prepared_input, "relevant_prior_coverage")
    assert not hasattr(prepared_input, "active_goal")
    assert not hasattr(prepared_input, "requirement_id")
    assert not hasattr(prepared_input, "research_goal")
    assert not hasattr(prepared_input, "options")


def test_task_group_carries_task_index_research_goal_and_options_only() -> None:
    """S1(選択肢の渡し方)。グループはtask_index/research_goal/optionsだけを持つ。"""
    _assert_frozen_slots_dataclass(EvidenceReviewTaskGroup)
    field_names = {field.name for field in fields(EvidenceReviewTaskGroup)}
    assert field_names == {"task_index", "research_goal", "options"}

    # research_goal は str 直値で持ち、標準の AnswerBrief field
    # (standalone_question 等)を型として持ち込めない。
    research_goal_field = next(
        field
        for field in fields(EvidenceReviewTaskGroup)
        if field.name == "research_goal"
    )
    assert research_goal_field.type in (str, "str")

    with pytest.raises(FrozenInstanceError):
        task_group(goal="goal-A").research_goal = "goal-B"


def test_review_input_rejects_content_requirements_as_a_construction_argument() -> None:
    """v3(判定基準の一本化)。content_requirementsはフィールドとして廃止され、

    構築時に渡そうとするとTypeErrorになる(研究目的=research_goalのみで
    判定する契約への回帰guard)。
    """
    with pytest.raises(TypeError):
        EvidenceReviewInput(
            task_groups=(task_group(),),
            content_requirements=("要件A",),
            as_of=AS_OF,
        )


# --- 投影と復元の往復 ---------------------------------------------------------


def test_preparation_resolves_shown_indexes_to_their_option_origins() -> None:
    """投影で見せた全ての通しindexが、渡した元の出所そのものへ解決される(往復)。

    投影と出所は同一の採番で作られるため、見せた番号と引ける番号はズレない。
    """
    internal = [
        internal_hit(assessment_id=1001, curation_id=1, title="A-int-1", summary="s"),
        internal_hit(assessment_id=1002, curation_id=2, title="A-int-2", summary="s"),
    ]
    external = [
        external_hit("https://example.com/b1", title="B-ext-1"),
        external_hit("https://example.com/b2", title="B-ext-2"),
    ]
    tasks = [
        collected_task(task_index=2, internal_hits=internal),
        collected_task(task_index=5, external_hits=external),
    ]

    preparation = EvidenceReviewPreparation.from_tasks(tasks)

    resolved = [
        preparation.resolve_option_origin(option.index)
        for group in preparation.task_groups
        for option in group.options
    ]
    assert [
        (origin.search_hit, origin.task_index) for origin in resolved if origin
    ] == [
        (internal[0], 2),
        (internal[1], 2),
        (external[0], 5),
        (external[1], 5),
    ]


def test_preparation_does_not_resolve_an_index_that_was_never_shown() -> None:
    """投影に載せていない番号は解決できずNoneになる(見せた番号だけが引ける)。

    負の番号もtupleの末尾参照に化けさせず構造的に弾く。
    """
    tasks = [
        sample_task(task_index=0, internal_count=1),
        sample_task(task_index=1, external_count=2),
    ]

    preparation = EvidenceReviewPreparation.from_tasks(tasks)

    shown_indexes = [
        option.index for group in preparation.task_groups for option in group.options
    ]
    assert (
        preparation.resolve_option_origin(max(shown_indexes) + 1),
        preparation.resolve_option_origin(-1),
    ) == (None, None)


# --- 見せた出典の一意 ---------------------------------------------------------


def test_preparation_keeps_the_first_external_hit_when_urls_repeat_within_a_task() -> (
    None
):
    """同じtaskの中ではURLは重複しない。先に並んでいたhitだけを見せる。"""
    first = external_hit("https://example.com/duplicate", title="first")
    second = external_hit("https://example.com/duplicate", title="second")
    other = external_hit("https://example.com/other", title="other")
    preparation = EvidenceReviewPreparation.from_tasks(
        [collected_task(task_index=0, external_hits=[first, second, other])]
    )

    options = preparation.task_groups[0].options
    first_origin = preparation.resolve_option_origin(0)
    other_origin = preparation.resolve_option_origin(1)

    assert [(option.index, option.title) for option in options] == [
        (0, "first"),
        (1, "other"),
    ]
    assert first_origin is not None
    assert other_origin is not None
    assert first_origin.search_hit is first
    assert other_origin.search_hit is other


def test_preparation_keeps_the_first_internal_hit_when_ids_repeat_within_a_task() -> (
    None
):
    """同じtaskの中では内部記事の識別IDは重複しない。先に並んでいたhitだけを見せる。"""
    first = internal_hit(assessment_id=1001, curation_id=42, title="first", summary="s")
    second = internal_hit(
        assessment_id=1002, curation_id=42, title="second", summary="s"
    )
    other = internal_hit(assessment_id=1003, curation_id=7, title="other", summary="s")
    preparation = EvidenceReviewPreparation.from_tasks(
        [collected_task(task_index=0, internal_hits=[first, second, other])]
    )

    options = preparation.task_groups[0].options
    first_origin = preparation.resolve_option_origin(0)

    assert [(option.index, option.title) for option in options] == [
        (0, "first"),
        (1, "other"),
    ]
    assert first_origin is not None
    assert first_origin.search_hit is first


def test_preparation_keeps_the_same_url_when_tasks_differ() -> None:
    """taskが違えば同じURLでも採用する。"""
    shared = "https://example.com/shared"
    preparation = EvidenceReviewPreparation.from_tasks(
        [
            collected_task(
                task_index=0, external_hits=[external_hit(shared, title="A")]
            ),
            collected_task(
                task_index=1, external_hits=[external_hit(shared, title="B")]
            ),
        ]
    )

    titles = [
        (option.index, option.title)
        for group in preparation.task_groups
        for option in group.options
    ]
    origin_a = preparation.resolve_option_origin(0)
    origin_b = preparation.resolve_option_origin(1)

    assert titles == [(0, "A"), (1, "B")]
    assert origin_a is not None
    assert origin_b is not None
    assert origin_a.task_index == 0
    assert origin_b.task_index == 1


def test_preparation_keeps_the_same_curation_id_when_tasks_differ() -> None:
    """taskが違えば同じ内部記事の識別IDでも採用する。"""
    preparation = EvidenceReviewPreparation.from_tasks(
        [
            collected_task(
                task_index=0,
                internal_hits=[
                    internal_hit(
                        assessment_id=1001, curation_id=42, title="A", summary="s"
                    )
                ],
            ),
            collected_task(
                task_index=1,
                internal_hits=[
                    internal_hit(
                        assessment_id=1002, curation_id=42, title="B", summary="s"
                    )
                ],
            ),
        ]
    )

    titles = [
        (option.index, option.title)
        for group in preparation.task_groups
        for option in group.options
    ]
    origin_a = preparation.resolve_option_origin(0)
    origin_b = preparation.resolve_option_origin(1)

    assert titles == [(0, "A"), (1, "B")]
    assert origin_a is not None
    assert origin_b is not None
    assert origin_a.task_index == 0
    assert origin_b.task_index == 1


# --- build_review_task_groups -----------------------------------------------


def test_task_groups_are_ordered_by_task_index_regardless_of_input_order() -> None:
    """グループの並びがtask_index昇順である(入力順に依存しない)。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        collected_task(task_index=1, research_goal="goal-B"),
        collected_task(task_index=0, research_goal="goal-A"),
    ]

    groups = from_tasks(tasks).task_groups

    assert [group.task_index for group in groups] == [0, 1]
    assert [group.research_goal for group in groups] == ["goal-A", "goal-B"]


def test_task_groups_place_internal_options_before_external_within_a_group() -> None:
    """各グループ内は内部の選択肢が先、外部の選択肢が後。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001,
                    curation_id=1,
                    title="internal-a",
                    summary="summary-a",
                ),
                internal_hit(
                    assessment_id=1002,
                    curation_id=2,
                    title="internal-b",
                    summary="summary-b",
                ),
            ],
            external_hits=[
                external_hit("https://example.com/x", title="external-x"),
                external_hit("https://example.com/y", title="external-y"),
            ],
        )
    ]

    groups = from_tasks(tasks).task_groups

    assert [(option.index, option.title) for option in groups[0].options] == [
        (0, "internal-a"),
        (1, "internal-b"),
        (2, "external-x"),
        (3, "external-y"),
    ]


def test_task_groups_assign_a_run_wide_index_without_duplication_across_groups() -> (
    None
):
    """indexがRun全体の通し番号であり、グループをまたいで重複しない。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        collected_task(
            task_index=0,
            internal_hits=[
                internal_hit(
                    assessment_id=1001, curation_id=1, title="A-int", summary="s"
                )
            ],
            external_hits=[external_hit("https://example.com/a", title="A-ext")],
        ),
        collected_task(
            task_index=1,
            internal_hits=[
                internal_hit(
                    assessment_id=1002, curation_id=2, title="B-int", summary="s"
                )
            ],
        ),
    ]

    groups = from_tasks(tasks).task_groups

    ordered = [
        (option.index, option.title) for group in groups for option in group.options
    ]
    assert ordered == [(0, "A-int"), (1, "A-ext"), (2, "B-int")]
    all_indexes = [option.index for group in groups for option in group.options]
    assert len(all_indexes) == len(set(all_indexes))


def test_task_groups_keep_a_task_with_no_options_as_an_empty_group() -> None:
    """選択肢が内外ともゼロのtaskもグループとして残る(欠番を作らない)。"""
    from_tasks = EvidenceReviewPreparation.from_tasks
    tasks = [
        collected_task(task_index=0, research_goal="goal-A"),
        collected_task(
            task_index=1,
            research_goal="goal-B",
            internal_hits=[
                internal_hit(
                    assessment_id=1001, curation_id=1, title="B-int", summary="s"
                )
            ],
        ),
        collected_task(task_index=2, research_goal="goal-C"),
    ]

    groups = from_tasks(tasks).task_groups

    assert [group.task_index for group in groups] == [0, 1, 2]
    assert groups[0].options == ()
    assert [option.index for option in groups[1].options] == [0]
    assert groups[2].options == ()


def test_task_groups_map_internal_source_name_to_none_and_truncate_body() -> None:
    """内部の選択肢: source_name=None、bodyはsummaryとkey_points連結をcapでtruncate。

    summary単体ではcapに届かない入力で、連結の実施と連結後のtruncateを区別する。
    """
    from_tasks = EvidenceReviewPreparation.from_tasks
    summary = "short summary"
    point = "p" * OPTION_BODY_MAX_CHARS
    hit = internal_hit(
        assessment_id=1001,
        curation_id=1,
        title="internal",
        summary=summary,
        key_points=[point],
        published_at=AS_OF,
    )
    tasks = [collected_task(task_index=0, internal_hits=[hit])]

    groups = from_tasks(tasks).task_groups

    option = groups[0].options[0]
    # 連結形式(summaryの次行に"- "付きkey_point)は投影仕様として直書きする。
    expected_body = f"{summary}\n- {point}"[:OPTION_BODY_MAX_CHARS]
    assert option.source_name is None
    assert option.published_at == AS_OF
    assert option.body == expected_body


def test_task_groups_truncate_external_body_to_the_review_budget() -> None:
    """外部の選択肢: 収集境界いっぱいの本文もReviewer予算まで切る。

    収集境界の上限とReviewerへ見せる予算は別の関心なので、投影は上流の値に
    依存せず自分の予算で切る。
    """
    from_tasks = EvidenceReviewPreparation.from_tasks
    collected_body = "x" * EXTERNAL_CONTENT_MAX_CHARS
    tasks = [
        collected_task(
            task_index=0,
            external_hits=[
                external_hit("https://example.com/long", content=collected_body)
            ],
        )
    ]

    groups = from_tasks(tasks).task_groups

    assert groups[0].options[0].body == collected_body[:OPTION_BODY_MAX_CHARS]


def test_task_groups_is_empty_tuple_when_there_are_no_tasks() -> None:
    from_tasks = EvidenceReviewPreparation.from_tasks

    assert from_tasks([]).task_groups == ()
