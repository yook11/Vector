# Agent PreparedAgentRun 削除 slice 仕様

更新日: 2026-08-19

実装状況: Implemented

## 位置付け

開始成功の戻りから `PreparedAgentRun` を外す。識別の正本は
`backend/specs/agent-run-identity-slice.md` の `RunIdentity`。
`attempt_epoch` は永続ランの fence であり、開始 outcome に載せる。
今の質問の `seq` は user message から読む。

歴史仕様の `PreparedAgentRun` 記述は一括置換しない。

## Work Definition

### Problem

- `PreparedAgentRun` が開始成功の戻りを「準備できたラン」として見せている。
- Identity 用 ID・今の質問・fence が同じ型に載っている。

### Evidence

- `PreparedAgentRun` は `backend/app/agent/runs/contracts.py` の frozen dataclass。
- `start_run()` が JOIN で question / seq / user_id を読み、同じ型に載せていた。
- worker は runner 直前に `RunIdentity` と `RunInput` へ写していた。
- `attempt_epoch` の正本は `agent_runs.attempt_epoch`。

### Invariants

- `RunIdentity` は `user_id` / `run_id` / `thread_id` / `as_of`。`attempt_epoch` は入れない。
- 1つの `AnsweringRunner.run()` に Identity は1つ。
- 開始成功の outcome は正の整数 `attempt_epoch` だけを返す。skip / 期限切れは `None`。
- `seq` は user message の属性。履歴窓は `seq < 今の番号`。
- `RunInput.question` は `str` のまま。`as_of` を切る位置は変えない。
- complete / fail / live / probe の fence は `run_id` + `attempt_epoch`。

### Non-goals

- DB / Alembic、公開 API。
- `RunInput` への seq 搭載、履歴 snapshot への seq 追加。
- 歴史仕様の一括置換、Redis 採番への移行。

### Done

- production から `PreparedAgentRun` / `prepared_run` が消える。
- worker は run/thread から Identity を組み、user message から本文と `seq` を読む。
- 開始・再取得・skip・timeout の振る舞いは同値。

## 契約

```python
@dataclass(frozen=True, slots=True)
class StartRunCommandOutcome:
    start_outcome: StartRunOutcome
    attempt_epoch: int | None
    quota_release_outcome: DailyQuotaReleaseOutcome | None


@dataclass(frozen=True, slots=True)
class UserQuestionMessage:
    content: str
    seq: int
```

worker:

```python
identity = RunIdentity(
    user_id=user_id,
    run_id=run_id,
    thread_id=thread_id,
    as_of=as_of,
)
```

`user_id` / `thread_id` は run と thread から読む。`content` / `seq` は
`AgentRun.user_message_id` 先の user message から読む。

## テスト所有

- `tests/agent/runs/_start_run_outcomes.py`: STARTED から正の epoch を取り出す。
- `tests/agent/test_agent_run_task.py`: Identity / history は runner 呼び出しで固定。
  再取得後の質問と seq は `read_user_question_for_run`。
- queued deadline / probe / quota テストは epoch の有無だけを見る。
