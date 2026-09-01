# Agent run progress_stage write removal slice

Status: Implemented — 2026-08-31

親仕様: `agent-run-progress-stage-slice.md`
前提: `../../frontend/specs/agent-research-live-stream-progress-read-slice.md`

## 位置付け

工程の表示正本は Redis Stream（SSE）である。本 slice は worker が
`agent_runs.progress_stage` を都度 UPDATE するのを止める。
列と API の `progressStage` は残し、`agent-run-progress-stage-drop-slice.md` が
アプリ・APIの参照を外した後、`z17_drop_progress_stage`で列を落とす。

## Work Definition

### Problem

frontend は工程を Stream だけから読む。worker はまだ工程ごとに短命 session で
`progress_stage` を書いており、表示に使われない DB 開閉が残る。

### Evidence

- `AgentRunLiveStageReporter` は `AgentRunProgressWriter` と Redis Stream へ fan-out する。
- `start_run` は `progress_stage=None` を同一 UPDATE で書く。
- complete / mark_failed は `progress_stage` を触らない。

### Invariants

1. 工程の SSE `stage` 配信は残す。activity List と execution probe は触らない。
2. `start_run` の `progress_stage=None` リセットは残す。
3. 実行中も終状態も `progress_stage` を書かない。新しい run は開始時 NULL のまま終わる。
4. stage 報告の失敗で run を落とさない。
5. 列・CHECK・`ResearchRunResponse.progress_stage` は残す。

### Non-goals

- 列 drop、API から `progressStage` 削除、2秒 polling 廃止、失敗時の再実行
- AnsweringRunner の `stage_changed` 語彙

### Done

- worker が `progress_stage` を UPDATE しない。
- 完了・失敗後の新しい run は `progress_stage is None`。
- SSE には従来どおり `stage` が載る。
