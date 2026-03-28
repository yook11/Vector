# ADR-001: タスクキューに taskiq を採用（arq を不採用）

> 日付: 2026-02-21 / ステータス: Accepted

## Context

バックグラウンドジョブ（RSS取得・AI分析・スケジュール実行）を処理するタスクキューが必要。
候補は arq と taskiq の2つ。両者で PoC を実施し比較評価した。

## Decision

**taskiq を採用。**

## Rationale

| 判断軸 | arq | taskiq |
|---|---|---|
| 開発状況 | maintenance-only (2025-02〜) | 活発 (v0.12.1, 2025-12) |
| リトライ | `arq.Retry` を明示 raise 必須 | `SimpleRetryMiddleware` + `retry_on_error=True` で自動 |
| ブローカー | Redis のみ | Redis / RabbitMQ / NATS / Kafka |
| 性能 | 標準 | 約10倍高速 |
| cron | 単一プロセスで完結 | 別プロセス（scheduler）が必要 |

**決め手**: arq がメンテナンスモードに入り長期運用に不安がある点。taskiq は cron に別プロセスが必要だが、Docker Compose で2コンテナ分離すれば問題ない。

## 実装時の注意点

- リトライ: `SimpleRetryMiddleware` と `retry_on_error=True` の**両方**が必要（片方だけでは発動しない）
- engine 共有: `Context = TaskiqDepends()` → `ctx.state.engine` パターンで connection pool を再利用
- Apple Silicon: `greenlet` の明示インストールが必要
- worker と scheduler は別コンテナに分離（プロセス監視のため `bash -c "... & ... & wait"` は不採用）

## 参考

- PoC 実施記録: git 履歴 `docs/archive/05b_TASKQUEUE_POC_REPORT.md`
