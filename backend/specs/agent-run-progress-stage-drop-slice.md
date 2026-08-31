# Agent run progress_stage drop slice

Status: Implemented — 2026-08-31

親仕様: `agent-run-progress-stage-slice.md`
前提: `agent-run-progress-stage-write-removal-slice.md`

## 位置付け

工程の表示正本は Redis Stream（SSE）である。worker の都度 UPDATE は止まっている。
本 slice は使われない `progress_stage` 列と API の `progressStage` を落とす。

## Work Definition

### Problem

frontend も worker も `agent_runs.progress_stage` を使わない。列・CHECK・
GET `/runs` と thread 詳細の `progressStage`・開始時 NULL リセットだけが残っている。

### Evidence

- live の工程は SSE `stage` だけが進める。
- `start_run` と `mark_policy_blocked` だけが列をまだ触る。
- `ResearchRunResponse` と `ResearchMessageRun` の両方に `progressStage` がある。

### Invariants

1. SSE `stage` と frontend live の `progressStage` は残す。
2. `AnswerProgressStage` は残す。
3. 当たった Alembic `z10_progress_stage_vocabulary` は書き換えない。
4. `start_run` の epoch 増分と `mark_policy_blocked` の終端は、列代入だけ外す。
5. 列の値は復元しない。downgrade は空の nullable 列を戻すだけ。

### Non-goals

- 2秒 polling、Redis List、execution probe、`research_checkpoint`
- 過去仕様ドキュメントの全文書き換え

### Done

- `agent_runs` に `progress_stage` 列と CHECK が無い。
- polling と thread 詳細の API に `progressStage` が無い。
- SSE の `stage` は従来どおり届く。
