# Agent Research live stream progress read slice

Status: Implemented — 2026-08-31

> 後続契約更新: `agent-research-sse-connected-no-poll-slice.md` は
> `connecting` / `live` / `reconnecting` 中の `GET /runs` を止める。
> `polling-only` の 2秒 poll は残す。本文中の「subscribe 開始から poll する」記述は無効である。
>
> 後続契約更新: `backend/specs/agent-run-recent-events-drop-slice.md` は
> Redis List publisher と `GET /runs` の `recentEvents` を落とす。
> 本文中の List / `recentEvents` 参照と Non-goals の「Redis List publisher 廃止」は無効である。

親仕様: `agent-research-live-ui-slice.md`

## 位置付け

工程・activity・回答下書きの表示正本を Redis Stream（SSE replay）だけにする。
polling は残し、status / attemptEpoch / 終状態の保険に限る。
backend の `progress_stage` 書き込みと `GET /runs` の response shape は触らない。

## Work Definition

### Problem

live の工程表示が SSE と polling / thread 詳細の `progressStage` の二系統になっている。
DB 工程を表示に使うと、次段で工程の DB 書き込みを止めたとき UI が壊れる。

### Evidence

- `createResearchRunLiveController` は `initialStage` で thread 詳細の工程を先塗りし、
  poll の `progressStage` / `recentEvents` を connecting と `polling-only` で採用する。
- Redis Stream は同一 attempt 内を `Last-Event-ID` で replay できる。
- EventSource `CLOSED` は SSE を張り直さず `polling-only` になる。このスライスでも張り直さない。

### Invariants

1. 工程・activity・下書きは SSE `stage` / `activity` / `answer.delta` だけが進める。
2. polling は `status`（queued→running の単調 merge）、正の `attemptEpoch`
   （未観測なら採用、より大きければ attempt-local を reset）、終状態だけを採用する。
3. thread 詳細の `progressStage` も poll の `progressStage` / `recentEvents` も表示に使わない。
4. 初期 `progressStage` は常に `null`（文言は「生成中」）。
5. `polling-only` では最後の SSE 工程を維持し、poll で工程・activity を更新しない。
6. 45秒 max age の `reconnecting` は同じ EventSource と `Last-Event-ID` のまま。
7. SSE 同士の遅延に対する `stageRank` 単調前進は残す。poll 由来の工程ではピン留めしない。

### Non-goals

- worker の `progress_stage` UPDATE 停止、列 drop、API から `progressStage` 削除
- 2秒 polling の廃止、CLOSED 時の EventSource 再作成
- 失敗時の再実行、Stream 欠落時の新 attempt
- execution probe 変更

### Done

- active run の工程・activity・下書きが SSE なしでは進まない。
- poll は終状態と attempt fencing と queued→running を継続する。
- 既存の terminal / reconnect / visibility 保証は維持する。
