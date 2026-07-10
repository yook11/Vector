# Agent live updates 境界分離 slice 仕様

## 位置付け

agent領域の責任整理を二段階で行ううち、本sliceは第一段階として、Redisを使う
一時的なlive表示・再生基盤を `app.agent.history` から分離する。

第二段階では、`history` に残る会話永続化とrunライフサイクルを
`conversations` / `runs` へ分ける。DB transaction境界を扱うため、本sliceには含めない。

## Problem

`app.agent.history` は現在、Postgresへ残す会話履歴に加えて、Redis Listによる
polling向け直近eventと、Redis StreamによるSSE向け再生ログを含んでいる。

永続履歴とlive updatesは、正本・保持期間・障害時の扱いが異なる。このまま同じ
packageへ追加を続けると、Redis transportの変更が履歴永続化の変更に見え、次の
`conversations` / `runs` 分割時にも境界が判別しにくい。

## Evidence

- `history/live_events.py` はRedis Listへ最大50件、TTL 900秒で保存し、polling APIへ
  最大10件を返すbest-effort transportである。
- `history/live_stream.py` はRedis Streamへ最大4096件、TTL 900秒で保存し、cursorと
  `attemptEpoch` による再生を行うbest-effort transportである。
- runtimeの直接参照はworker、research router、`history.__init__` に限定される。
- 対応テストは `test_agent_run_live_events.py` と
  `test_agent_run_live_stream.py` に分離されている。
- live transportはDB repositoryをimportしておらず、物理移動だけで分離できる。

## Target structure

```text
backend/app/agent/
├── history/                    # 本sliceでは既存のDB履歴・run管理を維持
└── live_updates/
    ├── __init__.py
    ├── recent_events.py        # Redis List / polling recentEvents
    └── stream.py               # Redis Stream / cursor replay

backend/tests/agent/
└── live_updates/
    ├── __init__.py
    ├── test_recent_events.py
    └── test_stream.py
```

## Invariants

- Postgresが会話・run状態・最終回答の唯一の正本である。
- Redis keyは `agent:run:{run_id}:events` と `agent:run:{run_id}:live` のまま変えない。
- List / StreamのTTL、件数上限、timeout、event schema、decode、best-effort契約を変えない。
- `attemptEpoch`、lazy marker retry、trim済みcursor、劣化結果型の契約を変えない。
- research APIのresponse shapeと `recentEvents` の意味を変えない。
- workerのmarker開始順序、run取得・完了・失敗のDB transactionを変えない。
- class・関数・定数名は変更せず、module pathだけを変更する。
- 旧 `app.agent.history.live_events` / `app.agent.history.live_stream` のcompatibility shimは
  残さない。repo内参照を全更新し、誤った境界への新規依存を防ぐ。

## Non-goals

- `AgentHistoryRepository` の分割・rename。
- `history/types.py`、`history/progress.py` の移動。
- `ThreadMessageSnapshot` の移動。
- `conversations` / `runs` packageの作成。
- DB schema・migration・Pydantic API schema・frontend型の変更。
- Redis ListとRedis Streamの統合、既存Listの廃止。
- SSE endpoint、BFF、browser UIの実装。

## Impact estimate

### Runtime

```text
move:   app/agent/history/live_events.py -> app/agent/live_updates/recent_events.py
move:   app/agent/history/live_stream.py -> app/agent/live_updates/stream.py
update: app/agent/router.py
update: app/queue/tasks/agent_run.py
update: app/agent/history/__init__.py
add:    app/agent/live_updates/__init__.py
```

### Tests

```text
move: tests/agent/test_agent_run_live_events.py -> tests/agent/live_updates/test_recent_events.py
move: tests/agent/test_agent_run_live_stream.py -> tests/agent/live_updates/test_stream.py
add:  tests/agent/live_updates/__init__.py
```

`test_agent_run_task.py` はworker module上のclass symbolをmonkeypatchしているため、
test本体のimport変更は不要である。

### Specifications

- `agent-run-live-events-slice.md` の配置パスを更新する。
- `agent-live-stream-transport-slice.md` の配置・テストパスを更新する。
- event contract、Redis contract、テスト期待値の本文は変更しない。

### Risk assessment

- 振る舞い変更リスク: 低。実装本体は内容を変えずに移動する。
- import漏れリスク: 中。repo全体の旧module path検索を完了条件に含める。
- test discoveryリスク: 低。移動先をPython package化し、対象テストを直接実行する。
- 外部consumerリスク: 低。このmoduleはbackend内部実装で、repo内参照は全件確認済み。

## Work plan

1. `live_updates` packageと対応test packageを作る。
2. Redis List実装を `recent_events.py`、Redis Stream実装を `stream.py` へ内容を
   変えずに移動する。
3. worker、router、tests、仕様書のimport / pathを更新する。
4. `history.__init__` からlive Stream exportを削除する。
5. `rg` で旧 `history.live_*` 参照と旧file pathが0件であることを確認する。
6. 対象unit / Redis integration、backend標準check、全integrationを実行する。

## Done

- Redis live transportが `app.agent.live_updates` のみに存在する。
- `app.agent.history` がlive publisher / readerをexportしていない。
- repo内に旧 `app.agent.history.live_events` / `live_stream` importが残っていない。
- Redis key・保持・event・attempt境界・劣化契約に差分がない。
- 対象テスト、backend lint / format、全integrationがgreenである。
- 第二段階の `conversations` / `runs` 分離へ手を広げていない。
