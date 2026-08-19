# Agent AnsweringRunContext 解体 slice 仕様

更新日: 2026-08-19

実装状況: Implemented

## 位置付け

識別は slice 1 の `RunIdentity`、後段への伝え方は slice 2 の `AnswerBrief`。
それらを束ねていた `AnsweringRunContext` と `RunResult.context` を外す。

前提: `backend/specs/agent-run-identity-slice.md`、
`backend/specs/agent-answer-brief-slice.md`。

## Work Definition

### Problem

- `AnsweringRunContext` が Identity と AnswerBrief と `previous_answer` を束ね、
  `RunResult.context` という名前で残っている。
- 識別・伝え方・履歴抜粋がまた Context に戻る。

### Evidence

- 袋は `running/contract.py` の frozen dataclass。中身は3 field。
- `AnsweringRunner.run()` が準備後に袋を組み、局所では袋経由で読んでいる。
- worker は `RunResult.final_output` と `research_checkpoint` だけを読む。
- `previous_answer` の消費者は DirectAnswerer だけ。

### Invariants

- 1 run で Identity は `run(identity=)` の1つ。Agent / Tool ごとに作り直さない。
- AnswerBrief は最大1回準備し、同じ instance を planner / answerer /
  `RunResult.answer_brief` へ渡す。
- Identity 全体は LLM に出さない。
- `previous_answer` は最新 assistant 本文を加工せず DirectAnswerer へ渡し、
  Brief にも result にも入れない。
- deps は composition / constructor のまま。

### Non-goals

- deps を袋へ移す。
- `context_preparer` 引数名、progress_stage / SSE / metric、
  package `question_context/`。
- `previous_answer` を Brief に入れること、API / DB、歴史仕様の一括置換。

### Done

- 公開名に `AnsweringRunContext` も `RunResult.context` も残らない。
- alias は置かない。

## 契約

```python
@dataclass(frozen=True, slots=True)
class RunResult:
    final_output: AnswerQuestionResult
    answer_brief: AnswerBrief
    research_checkpoint: ResearchCheckpoint | None = None
```

`identity` は `AnsweringRunner.run(..., identity=)` の引数。
`previous_answer` は DirectAnswerer への引数。

## テスト所有

- `tests/agent/running/test_contract.py`: 袋非公開、`RunResult` の3 field、
  旧 `context=` 拒否。
- `tests/agent/running/test_answering_runner.py`: 同じ AnswerBrief instance、
  previous_answer は DirectAnswerer 呼び出し。
- `tests/agent/test_agent_run_task.py`: Fake が袋を組まない。
