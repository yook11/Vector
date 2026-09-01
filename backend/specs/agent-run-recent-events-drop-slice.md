# Agent run recentEvents drop slice

Status: Implemented — 2026-09-01

親仕様: `agent-run-live-events-slice.md`
前提: `frontend/specs/agent-research-live-stream-progress-read-slice.md`

## 位置付け

activity の表示正本は Redis Stream（SSE）である。
Redis List への二重書きと `GET /runs` の `recentEvents` は消費者がいない。

## Work Definition

### Problem

worker が activity を Redis List へ書き、polling が `recentEvents` として読む。
frontend はすでにこの field を捨て、SSE 接続中は poll もしない。

### Evidence

- `AgentRunLiveActivityReporter` が List と Stream へ fan-out する。
- `get_research_run` が List を読んで `recent_events` を載せる。
- `parsePollRun` は `status` / `attemptEpoch` / `errorCode` だけを採る。

### Invariants

1. Redis Stream の activity / stage / delta / terminal と SSE は残す。
2. domain の `AnswerProgressEvent` と frontend の `ResearchLiveEvent` は残す。
3. `GET /runs` の `status` / `attemptEpoch` / `errorCode` は残す。
4. SSE endpoint と cancel の Redis は残す。
5. DB schema / Alembic は触らない。既存 List key は TTL で消える。

### Non-goals

- 2秒 polling の廃止、EventSource 再作成、生成中の打ち切り
- Stream / SSE / `progress_stage` 列 drop / execution probe
- 過去仕様ドキュメントの全文書き換え

### Done

- worker が Redis List に activity を書かない。
- `GET /runs` に `recentEvents` が無い。
- OpenAPI に List 専用 event schema が無い。
- SSE の activity は従来どおり届く。
