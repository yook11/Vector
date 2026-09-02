# Agent run worker self-stop

> 現行の処理・契約の配置は [Run周辺の概念境界](agent-run-concept-boundaries.md) を参照する。

Status: 実装済み

親仕様 [deadline と結果受理](agent-run-deadline-result-acceptance.md) の Slice 3。
前提は [deadline fence](agent-run-deadline-fence-slice.md) と [sweeper](agent-run-deadline-sweep-slice.md)。

## Problem / Evidence

`start_run` / `complete_run` / sweeper は `deadline_at` で受理を fence できるが、
実行中 worker は 150秒 monotonic で `failed / generation_unavailable` に落とす。
継続判定は `running + epoch` の bool だけで、期限切れを記録しない。

## 所有先

| 配置 | 責務 |
|---|---|
| `app/agent/runs/execution.py` | 1回の run 実行を続けてよいか。`Continue` / `Stop` / `StopReason` |
| `AgentRunRepository.decide_execution_continuation` | 判定。期限切れのときだけ `expire_run` を呼ぶ |
| `app/agent/run_deadline/persistence.py` | 単発の `deadline_exceeded` 確定（既存 `expire_run`） |
| `AgentRunExecutionProbe` | 2秒 cache と fail-open |
| `run_agent_answer` | runner 直前の確認、`Stop` の reason で SSE するか決める。terminal は書かない |

## Invariants

```text
continue
stop + deadline_exceeded   # expire_run に勝った
stop + not_current         # 書かない
```

- 判定時計は `database_now`。`now < deadline_at` だけ期限内。境界ちょうどは期限切れ。
- 新しい UPDATE 文は足さない。期限切れは `expire_run`。
- running 到達後の期限切れは quota を返さない。
- `AnswerGenerationStopped` は `failed` にしない。`deadline_exceeded` だけ SSE を送る。
- probe の 2秒 cache と fail-open（`Continue`）を維持する。delta ごとに新しい DB 時計は取らない。
- Taskiq 180秒は残す。その経路は `mark_failed` しない。

## Non-goals

- frontend 180秒、`mark_failed` への deadline fence。
- planner / search / review への continuation 貫通。
- 段階別 timeout、lease、quota 再設計。
- `complete_run` 受理境界の再実装。

## Done

1. `should_continue()` が `Continue | Stop` を返す。
2. 期限切れの DB 記録は `expire_run` だけ。
3. worker は runner 直前と既存の回答 stream hook で同じ判定を使う。
4. 150秒 application timeout と、その発火による `mark_failed(generation_unavailable)` が無い。
5. Taskiq 180秒は残る。
6. 既存 fence / sweep テストが落ちない。
