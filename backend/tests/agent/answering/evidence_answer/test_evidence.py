"""Answer evidence normalization tests(D4-S1)。

内部根拠は精査済み InternalArticleEvidence(claim付き)へ置換された。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.answering.evidence_answer.evidence import build_answer_input_evidence
from app.agent.contract import ExternalUrlSource
from app.agent.evidence_collection.external_search import ExternalSearchEvidence
from app.agent.evidence_review import AnswerEvidence
from app.agent.evidence_review.result import InternalArticleEvidence


def _outcome(
    *,
    internal_evidence: list[InternalArticleEvidence] | None = None,
    external_evidence: list[ExternalSearchEvidence] | None = None,
) -> AnswerEvidence:
    return AnswerEvidence(
        internal_articles=tuple(internal_evidence or []),
        external_sources=tuple(external_evidence or []),
    )


def _published_at(day: int) -> datetime:
    return datetime(2026, 7, day, 9, 0, tzinfo=UTC)


def _internal_evidence(
    *,
    task_index: int = 0,
    source_ref: str = "0-0",
    assessment_id: int,
    curation_id: int,
    title: str,
    claim: str = "内部claim",
    summary: str,
    key_points: list[str] | None = None,
    published_at: datetime | None = None,
) -> InternalArticleEvidence:
    return InternalArticleEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim=claim,
        why_selected="selection explanation not used for synthesis text",
        assessment_id=assessment_id,
        curation_id=curation_id,
        title=title,
        summary=summary,
        key_points=key_points or [],
        published_at=published_at,
    )


def _external_evidence(
    *,
    task_index: int = 0,
    source_ref: str,
    url: str,
    title: str,
    claim: str,
    snippet: str | None = None,
    published_at: datetime | None = None,
    source_name: str | None = None,
) -> ExternalSearchEvidence:
    return ExternalSearchEvidence(
        source_ref=source_ref,
        task_index=task_index,
        claim=claim,
        why_selected="selector explanation not used for synthesis text",
        url=url,
        title=title,
        snippet=snippet,
        published_at=published_at,
        source_name=source_name,
    )


def test_normalize_maps_all_internal_and_external_evidence_with_sequential_refs() -> (
    None
):
    internal_evidence = [
        _internal_evidence(
            source_ref="0-0",
            assessment_id=101,
            curation_id=1,
            title="OpenAI 半導体提携",
            summary="OpenAI が半導体供給網を強化した。",
        ),
        _internal_evidence(
            source_ref="0-1",
            assessment_id=102,
            curation_id=2,
            title="NVIDIA GPU 需要",
            summary="NVIDIA GPU の需要が拡大した。",
        ),
    ]
    external = [
        _external_evidence(
            source_ref="9-9",
            url="https://example.com/nvidia-1",
            title="NVIDIA official",
            claim="NVIDIA announced a new GPU platform.",
        ),
        _external_evidence(
            source_ref="1-0",
            url="https://example.com/nvidia-2",
            title="Supplier update",
            claim="A supplier reported higher AI demand.",
        ),
        _external_evidence(
            source_ref="1-1",
            url="https://example.com/nvidia-3",
            title="Cloud capex",
            claim="Cloud providers increased AI capex.",
        ),
    ]
    outcome = _outcome(
        internal_evidence=internal_evidence,
        external_evidence=external,
    )

    items = build_answer_input_evidence(outcome)

    assert len(items) == 5
    assert [item.source.source_ref for item in items] == ["1", "2", "3", "4", "5"]
    assert [item.source.kind for item in items] == [
        "internal_article",
        "internal_article",
        "external_url",
        "external_url",
        "external_url",
    ]


def test_normalize_preserves_internal_then_external_input_order() -> None:
    internal_evidence = [
        _internal_evidence(
            source_ref="0-0",
            assessment_id=101,
            curation_id=1,
            title="internal first",
            summary="first summary",
        ),
        _internal_evidence(
            source_ref="0-2",
            assessment_id=102,
            curation_id=2,
            title="internal second",
            summary="second summary",
        ),
    ]
    external = [
        _external_evidence(
            source_ref="0-3",
            url="https://example.com/first",
            title="external first",
            claim="first claim",
        ),
        _external_evidence(
            source_ref="0-1",
            url="https://example.com/second",
            title="external second",
            claim="second claim",
        ),
    ]

    items = build_answer_input_evidence(
        _outcome(
            internal_evidence=internal_evidence,
            external_evidence=external,
        )
    )

    assert [(item.source.source_ref, item.source.title) for item in items] == [
        ("1", "internal first"),
        ("2", "internal second"),
        ("3", "external first"),
        ("4", "external second"),
    ]


def test_normalize_preserves_external_provenance_and_uses_claim_as_evidence_claim() -> (
    None
):
    published_at = _published_at(4)
    evidence = _external_evidence(
        source_ref="9-9",
        url="https://example.com/nvidia",
        title="NVIDIA source",
        claim="NVIDIA introduced a new accelerator.",
        snippet="The accelerator targets AI inference.",
        published_at=published_at,
        source_name="Example News",
    )

    item = build_answer_input_evidence(
        _outcome(
            external_evidence=[evidence],
        )
    )[0]

    assert isinstance(item.source, ExternalUrlSource)
    assert str(item.source.url) == "https://example.com/nvidia"
    assert item.source.title == "NVIDIA source"
    assert item.source.evidence_claim == "NVIDIA introduced a new accelerator."
    assert item.source.published_at == published_at
    assert item.source.source_name == "Example News"


def test_normalize_preserves_internal_provenance_with_public_article_id() -> None:
    published_at = _published_at(5)
    evidence = _internal_evidence(
        assessment_id=301,
        curation_id=77,
        title="内部 NVIDIA 記事",
        summary="内部分析の要約。",
        published_at=published_at,
    )

    item = build_answer_input_evidence(_outcome(internal_evidence=[evidence]))[0]

    assert item.source.kind == "internal_article"
    assert item.source.article_id == 301
    assert item.source.title == "内部 NVIDIA 記事"
    assert item.source.published_at == published_at
    assert not hasattr(item.source, "snippet")
    assert not hasattr(item.source, "evidence_claim")
    assert not hasattr(item.source, "source_name")


def test_normalize_builds_internal_text_from_claim_then_summary_and_key_points() -> (
    None
):
    """保証するテスト条件 6。内部本文が claim + summary(+key_points) になる。"""
    internal_with_points = _internal_evidence(
        source_ref="0-2",
        assessment_id=401,
        curation_id=1,
        title="internal rich",
        claim="内部claim。",
        summary="内部要約。",
        key_points=["需要が増えた。", "供給制約が残る。"],
    )
    internal_without_points = _internal_evidence(
        source_ref="0-3",
        assessment_id=402,
        curation_id=2,
        title="internal plain",
        claim="内部claimのみ。",
        summary="内部要約のみ。",
    )
    external_with_snippet = _external_evidence(
        source_ref="0-0",
        url="https://example.com/with-snippet",
        title="external rich",
        claim="外部主張。",
        snippet="外部スニペット。",
    )
    external_without_snippet = _external_evidence(
        source_ref="0-1",
        url="https://example.com/no-snippet",
        title="external plain",
        claim="外部主張のみ。",
    )

    items = build_answer_input_evidence(
        _outcome(
            internal_evidence=[internal_with_points, internal_without_points],
            external_evidence=[external_with_snippet, external_without_snippet],
        )
    )

    assert [item.text for item in items] == [
        "内部claim。\n内部要約。\n- 需要が増えた。\n- 供給制約が残る。",
        "内部claimのみ。\n内部要約のみ。",
        "外部主張。\n外部スニペット。",
        "外部主張のみ。",
    ]


def test_normalize_ignores_external_local_source_ref() -> None:
    evidence = _external_evidence(
        source_ref="9-9",
        url="https://example.com/ref",
        title="external",
        claim="external claim",
    )

    item = build_answer_input_evidence(
        _outcome(
            external_evidence=[evidence],
        )
    )[0]

    assert item.source.source_ref == "1"


def test_normalize_ignores_internal_local_source_ref() -> None:
    evidence = _internal_evidence(
        source_ref="9-9",
        assessment_id=501,
        curation_id=1,
        title="internal",
        summary="summary",
    )

    item = build_answer_input_evidence(_outcome(internal_evidence=[evidence]))[0]

    assert item.source.source_ref == "1"


def test_normalize_omits_empty_external_snippet_from_text() -> None:
    evidence = _external_evidence(
        source_ref="0-0",
        url="https://example.com/empty-snippet",
        title="external",
        claim="external claim",
        snippet="",
    )

    item = build_answer_input_evidence(
        _outcome(
            external_evidence=[evidence],
        )
    )[0]

    assert item.text == "external claim"


def test_normalize_accepts_empty_and_partial_retrieval_outcomes() -> None:
    internal_evidence = _internal_evidence(
        assessment_id=601,
        curation_id=1,
        title="internal only",
        summary="summary",
    )
    assert build_answer_input_evidence(_outcome()) == []
    assert [
        item.source.source_ref
        for item in build_answer_input_evidence(
            _outcome(
                internal_evidence=[internal_evidence],
            )
        )
    ] == ["1"]
    assert build_answer_input_evidence(_outcome(external_evidence=None)) == []
    assert [
        item.source.source_ref
        for item in build_answer_input_evidence(
            _outcome(
                internal_evidence=[internal_evidence],
                external_evidence=[],
            )
        )
    ] == ["1"]
