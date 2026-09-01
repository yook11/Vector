# Agent run progress_stage drop slice

Status: Application cutover implemented — DB contract pending

親仕様: `agent-run-progress-stage-slice.md`
前提: `agent-run-progress-stage-write-removal-slice.md`

## 位置付け

工程の表示正本は Redis Stream（SSE）である。worker の都度 UPDATE は止まっている。
本 slice は先にアプリ・APIから`progress_stage`参照を外し、全ECS taskのrollout完了後に
後続contract migrationで列とCHECKを落とす。

## Work Definition

### Problem

frontend も worker も `agent_runs.progress_stage` を使わない。列・CHECK・
GET `/runs` と thread 詳細の `progressStage`・開始時 NULL リセットだけが残っている。
旧task稼働中の列削除を避けるため、アプリ切替とDB contractを別releaseにする。

### Evidence

- live の工程は SSE `stage` だけが進める。
- `start_run` と `mark_policy_blocked` だけが列をまだ触る。
- `ResearchRunResponse` と `ResearchMessageRun` の両方に `progressStage` がある。

### Invariants

1. SSE `stage` と frontend live の `progressStage` は残す。
2. `AnswerProgressStage` は残す。
3. 当たった Alembic `z10_progress_stage_vocabulary` は書き換えない。
4. `start_run` の epoch 増分と `mark_policy_blocked` の終端は、列代入だけ外す。
5. アプリ切替releaseではDB列とCHECKを残し、旧taskが0になるまでcontractを適用しない。

### Non-goals

- 2秒 polling、Redis List、execution probe、`research_checkpoint`
- 過去仕様ドキュメントの全文書き換え
- アプリ切替releaseでのDB列・CHECK削除

### Done

- アプリmodel・repository・projectionが`progress_stage`を参照しない。
- polling と thread 詳細の API に `progressStage` が無い。
- SSE の `stage` は従来どおり届く。
- DB列とCHECKは後続contract migrationまで残る。
