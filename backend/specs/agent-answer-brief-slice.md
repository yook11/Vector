# Agent AnswerBrief slice 仕様

更新日: 2026-08-27

実装状況: Superseded

`AnswerBrief` と question_context 工程は撤去された。現在の正本は
`specs/agent/research-handoff.md`。以下は撤去前の記録として残す。

## 位置付け

後段モデルへ渡す「質問の整理済みの伝え方」を、`QuestionContext` / `context` から
`AnswerBrief` / `answer_brief` へ移す。識別は slice 1 の `RunIdentity`。袋の解体は
`backend/specs/agent-answering-run-context-dissolve-slice.md`。

生成工程の置き場（package `question_context/`、agent name、progress_stage、
SSE event、metric 名）は動かない。

前提: `backend/specs/agent-run-identity-slice.md`。

## Work Definition

### Problem

- 消費者契約が `QuestionContext` / `context` と呼ばれ、RunIdentity や
  AnsweringRunContext と Context が重なる。
- `PlanningRequest.context` では、何を載せているか型を見ないと分からない。

### Evidence

- 4フィールドの正本は `question_context/contract.py` の `QuestionContext`。
- planner / answerer は `PlanningRequest.context` / `AnsweringRequest.context` を読む。
- `AnsweringRunContext.question_context` が同じ instance を保持する。
- renderer は `request.context.*` から prompt 文字列を作る。instructions と
  template 見出しは Prompt version の対象である。

### Invariants

- 4フィールドの意味・上限・正規化（`answer_brief_from_draft`）は同値。
- 1 run で最大1回準備し、同じ instance を planner / answerer へ渡す。
- `AnswerBrief` を Identity に混ぜない。`previous_answer` を Brief に入れない。
- prompt の model-visible 本文と version は変えない。
- `QuestionContext` の alias は置かない。フィールド名は `brief` ではなく
  `answer_brief` とする。

### Non-goals

- package `question_context/` の移動。
- `QUESTION_CONTEXT_AGENT` / `QuestionContextService` /
  `QuestionContextGenerationInput` / `render_question_context_input` の改名。
- progress_stage `context_resolution`、SSE `context_resolution.question_resolved`、
  metric 名。
- `AnsweringRunContext` 解体、API / DB、歴史仕様の一括置換。

### Done

- 消費者契約から `QuestionContext` と request の `context` が消え、
  型 `AnswerBrief` / フィールド `answer_brief` になる。
- contract / hooks / runner テストが field 名と同一 instance 引き渡しを固定する。

## 契約

```python
class AnswerBrief(BaseModel):
    standalone_question: StandaloneQuestion
    answer_requirements: tuple[AnswerRequirementText, ...] = ()
    relevant_prior_coverage: RelevantPriorCoverage = ""
    active_goal: ActiveGoal = ""


class PlanningRequest(BaseModel):
    answer_brief: AnswerBrief
    as_of: datetime
    prior_research: tuple[ResearchCheckpoint, ...] = ()


class AnsweringRequest(BaseModel):
    answer_brief: AnswerBrief
    as_of: datetime


class AnsweringRunContext:
    identity: RunIdentity
    answer_brief: AnswerBrief
    previous_answer: str
```

`AnswerBriefPreparer.prepare(...) -> AnswerBrief`。
hook は `on_answer_brief_prepared(..., answer_brief=)`。

## テスト所有

- `tests/agent/question_context/test_contract.py`: 4 field / extra forbid / from_draft。
- planning / answering `test_contract.py`: request field 名が `answer_brief`。
- `tests/agent/running/test_contract.py`: `AnsweringRunContext.answer_brief`。
- `tests/agent/running/test_hooks.py`: `on_answer_brief_prepared`。
- `tests/agent/running/test_answering_runner.py`: 同一 instance。
