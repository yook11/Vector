"""External search のドメイン純関数契約(D4-S1)。

selector 一式(build_external_evidence / finalize_selection_draft /
EVIDENCE_SELECT_TIMEOUT_SECONDS 等)は `evidence_review.policy` へ改名移設
された(tests/agent/evidence_collection/evidence_review/test_policy.py が
新契約の正本)。ここには外部候補pool構築・外部URL dedupなど、query/candidate
収集に閉じた関数だけを残す。
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest

from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchCandidate,
    ExternalSearchEvidence,
)


def _policy() -> ModuleType:
    try:
        return import_module("app.agent.evidence_collection.external_search.policy")
    except ModuleNotFoundError as exc:
        pytest.fail(
            "external search のドメイン純関数は policy module に置く必要があります "
            f"({exc.name})",
            pytrace=False,
        )


def _function(name: str) -> Any:
    value = getattr(_policy(), name, None)
    if value is None:
        pytest.fail(f"policy must export {name}", pytrace=False)
    return value


def _candidate(url: str, *, title: str | None = None) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="snippet",
        source_name="Example",
        published_at=datetime(2026, 7, 19, tzinfo=UTC),
    )


def _evidence(*, task_index: int, source_ref: str, url: str) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim="claim",
        why_selected="why",
        url=url,
        title=source_ref,
    )


def test_policy_exports_the_public_domain_functions_and_timeout_constants() -> None:
    policy = _policy()

    assert (
        {
            "clean_generated_queries",
            "build_candidate_pool",
            "deduplicate_external_evidence_by_url",
            "resolve_external_search_agent_count",
        }
        <= set(dir(policy)),
        policy.QUERY_GENERATE_TIMEOUT_SECONDS,
        policy.PROVIDER_SEARCH_TIMEOUT_SECONDS,
    ) == (True, 30, 15)


def test_clean_generated_queries_strips_caps_deduplicates_and_limits_to_three() -> None:
    clean_generated_queries = _function("clean_generated_queries")
    overlong = "x" * 205

    assert clean_generated_queries(
        ["  NVIDIA  ", "NVIDIA", "", overlong, "B", "C"]
    ) == [
        "NVIDIA",
        "x" * 200,
        "B",
    ]


def test_build_candidate_pool_round_robins_urls_and_stops_at_twenty() -> None:
    build_candidate_pool = _function("build_candidate_pool")
    query_candidates = [
        [
            _candidate("https://example.com/shared", title="first shared"),
            *[
                _candidate(f"https://example.com/left-{index}")
                for index in range(1, 20)
            ],
        ],
        [
            _candidate("https://example.com/shared", title="second shared"),
            *[
                _candidate(f"https://example.com/right-{index}")
                for index in range(1, 20)
            ],
        ],
    ]

    pool = build_candidate_pool(query_candidates)

    assert [candidate.title for candidate in pool[:6]] == [
        "first shared",
        "left-1",
        "right-1",
        "left-2",
        "right-2",
        "left-3",
    ]
    assert len(pool) == 20


def test_deduplicate_external_evidence_by_url_keeps_first_source_ref() -> None:
    deduplicate = _function("deduplicate_external_evidence_by_url")
    first = _evidence(
        task_index=0,
        source_ref="0-0",
        url="https://example.com/shared",
    )
    duplicate = _evidence(
        task_index=1,
        source_ref="1-0",
        url="https://example.com/shared",
    )
    unique = _evidence(
        task_index=1,
        source_ref="1-1",
        url="https://example.com/unique",
    )

    evidence, dropped = deduplicate([first, duplicate, unique])

    assert (evidence, dropped) == ([first, unique], 1)


def test_external_agent_count_is_bounded_by_task_count_and_hard_limit() -> None:
    resolve_agent_count = _function("resolve_external_search_agent_count")

    assert [
        resolve_agent_count(task_count=task_count, requested_agent_count=requested)
        for task_count, requested in [
            (0, None),
            (1, None),
            (2, None),
            (4, None),
            (4, 4),
            (1, 3),
            (2, 0),
            (2, -1),
        ]
    ] == [0, 1, 2, 3, 3, 1, 1, 1]
