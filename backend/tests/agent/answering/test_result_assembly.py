"""Result assembly が過去の external failure DTO を解釈する契約。"""

from __future__ import annotations

from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.result_assembly import assemble_evidence_result
from app.agent.contract import InternalArticleSource
from app.agent.evidence_collection import EvidenceCollectionOutcome
from app.agent.planning.contract import (
    ExternalResearchTask,
    InternalAndExternalPlan,
)
from app.agent.question_context import QuestionContext


def test_assembly_caps_answered_draft_for_historical_external_failure() -> None:
    context = QuestionContext(standalone_question="NVIDIA の見通しは？")
    plan = InternalAndExternalPlan(
        internal_queries=["NVIDIA"],
        external_research_tasks=[
            ExternalResearchTask(collection_goal="供給を確認する")
        ],
        target_time_window="直近24時間",
        reason="both evidence sources are required",
    )
    evidence = [
        AnswerEvidenceItem(
            source=InternalArticleSource(
                source_ref="1",
                article_id=1001,
                title="internal evidence",
            ),
            text="internal evidence",
        )
    ]

    result = assemble_evidence_result(
        context=context,
        plan=plan,
        outcome=EvidenceCollectionOutcome(collection_failures=["external_search"]),
        evidence=evidence,
        draft=EvidenceAnswerDraft(
            sufficiency="answered",
            answer="内部根拠の範囲では確認できます。",
            cited_refs=["1"],
        ),
    )

    assert (
        result.status,
        result.answer,
        result.retrieval.collection_failures,
        result.missing_aspects,
    ) == (
        "insufficient",
        "内部根拠の範囲では確認できます。",
        ["external_search"],
        ["外部検索を完了できませんでした"],
    )
