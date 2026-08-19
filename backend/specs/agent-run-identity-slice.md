# Agent RunIdentity 共有 slice 仕様

更新日: 2026-08-19

実装状況: Implemented

## 位置付け

回答 Run の識別と基準時刻を、OpenAI SDK 由来の `RunContext` から
`RunIdentity` へ移す。worker が既に持つ `user_id` / `thread_id` を同じ袋に載せ、
後段が共有できるようにする。

`QuestionContext` の改名は `backend/specs/agent-answer-brief-slice.md`。
`AnsweringRunContext` の解体は `backend/specs/agent-answering-run-context-dissolve-slice.md`。
deps の寄せは後続 slice。

前提: `backend/specs/agent-answering-runner-boundary-slice.md` の
`RunContext` / `AnsweringRunContext` 境界。本 slice 以降、識別の正本は
`RunIdentity` とする。歴史仕様の `RunContext` 記述は一括置換しない。

## Work Definition

### Problem

- 識別と基準時刻が `RunContext` という名前で、`run_id` と `as_of` しか持たない。
- `user_id` / `thread_id` は `PreparedAgentRun` に残り、AnsweringRunner 境界と共有できない。

### Evidence

- `RunContext` は `backend/app/agent/running/contract.py` の frozen dataclass。
- worker (`run_agent_answer`) は `PreparedAgentRun` から `run_id` だけを `RunContext` へコピーする。
- `PreparedAgentRun` は `user_id` / `thread_id` / `run_id` / `attempt_epoch` を既に持つ。
- `AnsweringRunner.run()` は `run_id` と `as_of` だけを読む。
- `attempt_epoch` は live stream / continuation probe の fence であり、Runner は消費しない。

### Invariants

- 1つの `AnsweringRunner.run()` invocation で `RunIdentity` は1つだけ。Agent / Tool
  ごとに作り直さない。
- `RunIdentity` 全体を LLM へ送らない。`as_of` は時制が必要な工程へ明示コピーする。
- `attempt_epoch` を `RunIdentity` に入れない。
- Runner は `run_id` と `as_of` だけを読む。`user_id` / `thread_id` は保持し、
  `RunResult.context.identity` まで同じ instance を運ぶ。
- `QuestionContext`、`AnsweringRunContext` 袋、deps の置き場を変えない。
- `RunContext` の alias と `run_context=` 互換は置かない。

### Non-goals

- `QuestionContext` → `AnswerBrief`。
- `AnsweringRunContext` の解体。
- progress_stage 名、API / DB / Alembic。
- 歴史仕様の `RunContext` 記述の一括置換。
- deps を Identity へ移すこと。

### Done

- worker が `PreparedAgentRun` + `as_of` から `RunIdentity` を1つ作り、
  `AnsweringRunner.run(identity=...)` に渡す。
- 公開名に `RunContext` が残らない。
- contract / runner / worker テストが Identity の4 field と同一 instance 引き渡しを固定する。

## 契約

```python
@dataclass(frozen=True, slots=True)
class RunIdentity:
    user_id: UUID
    run_id: UUID
    thread_id: UUID
    as_of: datetime


@dataclass(frozen=True, slots=True)
class AnsweringRunContext:
    identity: RunIdentity
    question_context: QuestionContext
    previous_answer: str
```

`AnsweringRunner.run(input, *, identity: RunIdentity, hooks=None)`。

worker:

```python
identity=RunIdentity(
    user_id=prepared.user_id,
    run_id=prepared.run_id,
    thread_id=prepared.thread_id,
    as_of=as_of,
)
```

## テスト所有

- `tests/agent/running/test_contract.py`: 4 field、frozen、`attempt_epoch` なし、
  `AnsweringRunContext.identity` の型。
- `tests/agent/test_agent_run_task.py`: worker が prepared の user/thread/run と
  UTC `as_of` を同じ Identity として渡す。
- `tests/agent/running/test_answering_runner.py`:
  `result.context.identity is identity`。
