# Agent Research SSE connected no-poll slice

Status: Implemented — 2026-09-01

親仕様: `agent-research-live-stream-progress-read-slice.md`

## 位置付け

SSE がつながっている間とブラウザが張り直し中は `GET /runs` を打たない。
EventSource が完全に止まったあとの 2秒 poll は残す。

## Work Definition

### Problem

live controller は subscribe 開始から 2秒ごとに DB を見ている。
工程も終状態もこの間は SSE が運ぶので、その GET は無駄である。

### Evidence

- `startLifecycle` が無条件に `startPoll` する。
- `connecting` / `live` / `reconnecting` でも poll が続く。
- `CLOSED` は `polling-only` になり、EventSource を張り直さない。

### Invariants

1. `connecting` / `live` / `reconnecting` では `pollRun` を呼ばない。
2. `polling-only` のあと、既存どおり 2秒 poll する。
3. SSE `terminal`、thread 再取得、同じ EventSource の再接続、下書き suppress は変えない。

### Non-goals

- `polling-only` の廃止、CLOSED 後の一度だけ確認、生成中の打ち切り
- EventSource の手動再作成、GET `/runs` 削除、`recentEvents` 削除

### Done

- SSE 接続中と張り直し中に `GET /runs` が走らない。
- `polling-only` では従来どおり 2秒 poll が始まる。
