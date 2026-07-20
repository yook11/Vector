"""External search のドメイン純関数契約。"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from types import ModuleType
from typing import Any

import pytest

from app.agent.evidence_collection.external_search.contract import (
    EvidenceSelectionResult,
    ExternalEvidenceSelectionDraft,
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
            "build_external_evidence",
            "deduplicate_external_evidence_by_url",
            "finalize_selection_draft",
            "resolve_provider_failure_reason",
            "resolve_external_search_agent_count",
        }
        <= set(dir(policy)),
        policy.QUERY_GENERATE_TIMEOUT_SECONDS,
        policy.PROVIDER_SEARCH_TIMEOUT_SECONDS,
        policy.EVIDENCE_SELECT_TIMEOUT_SECONDS,
        policy.SELECTOR_TIMEOUT_REASON,
        policy.SELECTOR_ERROR_REASON,
    ) == (True, 30, 15, 30, "selector_timeout", "selector_error")


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


def test_build_evidence_restores_metadata_and_drops_invalid_indexes() -> None:
    build_external_evidence = _function("build_external_evidence")
    pool = [
        _candidate("https://example.com/first", title="first title"),
        _candidate("https://example.com/second", title="second title"),
    ]
    selection = EvidenceSelectionResult.from_raw(
        selections=[
            {"candidate_index": 1, "claim": "second", "why_selected": "why"},
            {"candidate_index": 1, "claim": "duplicate", "why_selected": "why"},
            {"candidate_index": 99, "claim": "invalid", "why_selected": "why"},
            {"candidate_index": 0, "claim": "first", "why_selected": "why"},
        ],
        missing=[],
    )

    evidence, dropped = build_external_evidence(
        task_index=3,
        pool=pool,
        selection_result=selection,
    )

    assert (
        [(item.source_ref, str(item.url), item.title) for item in evidence],
        dropped,
    ) == (
        [
            ("external-3-1", "https://example.com/second", "second title"),
            ("external-3-0", "https://example.com/first", "first title"),
        ],
        2,
    )


def test_build_evidence_keeps_only_the_first_five_valid_selections() -> None:
    build_external_evidence = _function("build_external_evidence")
    pool = [
        _candidate(f"https://example.com/{index}", title=f"candidate {index}")
        for index in range(7)
    ]
    selection = EvidenceSelectionResult.from_raw(
        selections=[
            {
                "candidate_index": index,
                "claim": f"claim {index}",
                "why_selected": "why",
            }
            for index in [2, 0, 99, 2, 1, 3, 4, 5, 6]
        ],
        missing=[],
    )

    evidence, dropped = build_external_evidence(
        task_index=0,
        pool=pool,
        selection_result=selection,
    )

    assert (
        [item.source_ref for item in evidence],
        dropped,
    ) == (
        [
            "external-0-2",
            "external-0-0",
            "external-0-1",
            "external-0-3",
            "external-0-4",
        ],
        4,
    )


def test_deduplicate_external_evidence_by_url_keeps_first_source_ref() -> None:
    deduplicate = _function("deduplicate_external_evidence_by_url")
    first = _evidence(
        task_index=0,
        source_ref="external-0-0",
        url="https://example.com/shared",
    )
    duplicate = _evidence(
        task_index=1,
        source_ref="external-1-0",
        url="https://example.com/shared",
    )
    unique = _evidence(
        task_index=1,
        source_ref="external-1-1",
        url="https://example.com/unique",
    )

    evidence, dropped = deduplicate([first, duplicate, unique])

    assert (evidence, dropped) == ([first, unique], 1)


def test_finalize_selection_draft_clamps_values_to_existing_contract() -> None:
    finalize_selection_draft = _function("finalize_selection_draft")
    draft = ExternalEvidenceSelectionDraft.model_validate(
        {
            "selections": [
                {
                    "candidate_index": 0,
                    "claim": "c" * 350,
                    "why_selected": "w" * 350,
                }
            ],
            "missing": ["m" * 250],
        }
    )

    result = finalize_selection_draft(draft)

    assert (
        len(result.selections[0].claim),
        len(result.selections[0].why_selected),
        len(result.missing[0]),
    ) == (300, 300, 200)


def test_provider_failure_reason_prefers_reason_then_code_then_safe_fallback() -> None:
    resolve_failure_reason = _function("resolve_provider_failure_reason")

    assert (
        resolve_failure_reason(reason="timeout", code="ai_error_network"),
        resolve_failure_reason(reason=None, code="ai_error_network"),
        resolve_failure_reason(reason=None, code=None),
    ) == ("timeout", "ai_error_network", "selector_error")


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
