# Agent run progress_stage drop slice

Status: Contract migration implemented — production application pending

親仕様: `agent-run-progress-stage-slice.md`
前提: `agent-run-progress-stage-write-removal-slice.md`

## 位置付け

工程の表示正本は Redis Stream（SSE）である。worker の都度 UPDATE は止まっている。
アプリ・APIからの`progress_stage`参照は先行releaseで外す。本sliceは全ECS taskの
rollout完了後に列とCHECKを落とすcontract migrationを定義する。

## Work Definition

### Problem

frontend も worker も `agent_runs.progress_stage` を使わない。列・CHECK・
GET `/runs` と thread 詳細の `progressStage`・開始時 NULL リセットだけが残っている。
旧task稼働中の列削除を避けるため、DB contractはアプリ切替後にだけ適用する。

### Evidence

- live の工程は SSE `stage` だけが進める。
- `start_run` と `mark_policy_blocked` だけが列をまだ触る。
- `ResearchRunResponse` と `ResearchMessageRun` の両方に `progressStage` がある。

### Invariants

1. SSE `stage` と frontend live の `progressStage` は残す。
2. `AnswerProgressStage` は残す。
3. 当たった Alembic `z10_progress_stage_vocabulary` は書き換えない。
4. `start_run` の epoch 増分と `mark_policy_blocked` の終端は、列代入だけ外す。
5. `z17_drop_progress_stage`は`z16_thread_research_handoff`を親とするcontract migrationにする。
6. 先行releaseへの全task収束と旧task 0件を確認するまでcontractを適用しない。
7. 列の値は復元しない。downgradeは空のnullable列とCHECKを戻すだけとする。

### Non-goals

- 2秒 polling、Redis List、execution probe、`research_checkpoint`
- 過去仕様ドキュメントの全文書き換え
- 先行releaseのECS rollout
- contract migrationの自動実行

### Done

- アプリmodel・repository・projectionが`progress_stage`を参照しない。
- polling と thread 詳細の API に `progressStage` が無い。
- SSE の `stage` は従来どおり届く。
- `z17_drop_progress_stage`のupgradeでDB列とCHECKが無くなる。
- downgradeで空のnullable列とCHECKだけが戻る。
