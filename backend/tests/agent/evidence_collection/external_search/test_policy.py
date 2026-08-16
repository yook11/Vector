"""External search のドメイン純関数契約(D4-S1)。

selector 一式は `evidence_review` packageが所有する。ここには外部ヒットpool構築など、
query/hit収集に閉じた関数だけを残す。外部URL dedupは
S1(仕様「合流と重複排除」)で廃止された。
"""

from __future__ import annotations

from datetime import UTC, datetime

import app.agent.evidence_collection.external_search.policy as policy_module
from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchHit,
)
from app.agent.evidence_collection.external_search.policy import (
    PROVIDER_SEARCH_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
    build_hit_pool,
    clean_generated_queries,
    resolve_external_search_agent_count,
)


def _hit(url: str, *, title: str | None = None) -> ExternalSearchHit:
    return ExternalSearchHit(
        url=url,
        title=title or url.rsplit("/", maxsplit=1)[-1],
        snippet="snippet",
        source_name="Example",
        published_at=datetime(2026, 7, 19, tzinfo=UTC),
    )


def test_policy_exports_the_public_domain_functions_and_timeout_constants() -> None:
    assert (
        {
            "clean_generated_queries",
            "build_hit_pool",
            "resolve_external_search_agent_count",
        }
        <= set(dir(policy_module)),
        QUERY_GENERATE_TIMEOUT_SECONDS,
        PROVIDER_SEARCH_TIMEOUT_SECONDS,
    ) == (True, 30, 15)


def test_url_deduplication_is_removed_from_policy() -> None:
    """S1(合流と重複排除)。外部根拠のURL重複排除は廃止され、taskが違えば

    同じURLが別の観点の根拠として並ぶことを許容する
    (deduplicate_external_evidence_by_url()とその整合validatorを削除する)。
    """
    assert not hasattr(policy_module, "deduplicate_external_evidence_by_url")


def test_clean_generated_queries_strips_caps_deduplicates_and_limits_to_three() -> None:
    overlong = "x" * 205

    assert clean_generated_queries(
        ["  NVIDIA  ", "NVIDIA", "", overlong, "B", "C"]
    ) == [
        "NVIDIA",
        "x" * 200,
        "B",
    ]


def test_build_hit_pool_round_robins_urls_and_stops_at_twenty() -> None:
    hits_by_query = [
        [
            _hit("https://example.com/shared", title="first shared"),
            *[_hit(f"https://example.com/left-{index}") for index in range(1, 20)],
        ],
        [
            _hit("https://example.com/shared", title="second shared"),
            *[_hit(f"https://example.com/right-{index}") for index in range(1, 20)],
        ],
    ]

    pool = build_hit_pool(hits_by_query)

    assert [hit.title for hit in pool[:6]] == [
        "first shared",
        "left-1",
        "right-1",
        "left-2",
        "right-2",
        "left-3",
    ]
    assert len(pool) == 20


def test_external_agent_count_is_bounded_by_task_count_and_hard_limit() -> None:
    assert [
        resolve_external_search_agent_count(
            task_count=task_count, requested_agent_count=requested
        )
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
