# Agent threads / runs 境界分離 slice 仕様

> 後続契約更新: `agent-input-safety-gate-slice.md` はrun terminal statusへ `policy_blocked`を追加する。
> `completed ⇔ assistant_message_id` invariantは維持し、policy blockedではassistant messageを作らない。

## 位置付け

agent領域の責任整理の第二段階として、第一段階後の `app.agent.history` を
`app.agent.threads` と `app.agent.runs` に分離する。

第一段階で作成した `app.agent.live_updates` は変更せず、最終的に次の3境界とする。

```text
app/agent/
├── threads/        # thread / message / sourceと会話read model
├── runs/            # run lifecycle / attempt / progress
└── live_updates/    # Redis上の一時的な表示・再生transport
```

## Problem

`AgentHistoryRepository` は、会話履歴の読取とrun状態機械のcommandを同じclassで扱う。
また `ThreadMessageSnapshot` がrepository moduleに定義され、question resolutionの
contractが永続化実装moduleへ依存している。

一方、`create_user_run()` と `complete_run()` はrunとthread/messageの複数tableを同一
transactionで更新する。この原子性を無視してtable単位にrepositoryを分けると、user message
だけ、またはassistant messageだけが残る部分成功を生み得る。

## Evidence

- `create_user_run()` はthread lock、active run確認、user message、run作成を同一sessionで行う。
- `complete_run()` はthread lock、assistant message、source、run completed遷移を同一sessionで行う。
- repository自身は `commit()` せず、router / workerが `session.begin()` を所有する。
- `read_recent_messages_before()` はquestion resolution用のbounded readで、run状態を更新しない。
- thread list / detail / deleteはthread APIのread・管理責務である。
- mark failed / enqueue failed / acquire / cancel / stale sweepはrun状態機械の責務である。
- DB制約がactive run一意性、run-message同一thread、completed-answer整合を保証している。

## Repository boundary decision

repositoryはtable単位ではなく、transactionを完結させるユースケース単位で分ける。

### `AgentRunRepository`

```text
create_user_run
mark_failed
mark_enqueue_failed
acquire_for_execution
complete_run
read_run_for_user
cancel_run_for_user
sweep_stale_runs
```

`create_user_run` / `complete_run` はthread/message tableも更新するが、run開始・完了commandの
原子性を所有するためrun側へ置く。repository間の細粒度な相互呼び出しや新しいservice層は
追加しない。

### `AgentThreadRepository`

```text
read_recent_messages_before
list_threads_for_user
read_thread_detail_for_user
delete_thread_for_user
```

thread detailは関連runをbatch queryしてpublic read modelを構築する。これはthread画面の
read projectionであり、N+1を避ける現在のquery形を維持する。

## Target structure

```text
backend/app/agent/threads/
├── __init__.py
├── contracts.py              # ThreadMessageSnapshot
├── projection.py             # thread/message/source responses
└── repository.py             # thread read / management

backend/app/agent/runs/
├── __init__.py
├── citation_integrity.py     # completed answer/source整合
├── contracts.py              # errors / Created / Prepared / Cancel outcome
├── progress.py
├── projection.py             # run response / message run projection
├── repository.py             # run lifecycle commands
├── result_mapper.py          # completed result -> message/source rows
└── types.py                  # status / error code / progress stage
```

package `__init__.py` はdocstringだけとし、別責務を一つのfacadeへ再集約しない。consumerは
必要なsubmoduleを明示的にimportする。

## Invariants

- queryのWHERE、JOIN、ORDER BY、LIMIT、lock、batch取得数を変更しない。
- repository内で `commit()` しない。既存router / workerの `session.begin()` を維持する。
- `create_user_run()` はthread/user message/runの作成を同一transactionで行う。
- `complete_run()` はassistant message/source/run completed遷移を同一transactionで行い、
  transition race時は全追加行をrollbackする。
- active run一意性、所有権確認、terminal idempotency、cancel競合、stale判定を変えない。
- thread listの最終活動順、pagination、active run投影を変えない。
- thread detailはmessage順、run/sourceのbatch query、source ordinal順を変えない。
- bounded historyは `seq < before_seq`、降順LIMIT後の昇順返却を変えない。
- public API response、DB schema、Redis契約、task名、routeを変えない。
- class名以外のrepository method名と引数・戻り値を変えない。
- 旧 `app.agent.history` compatibility shimは残さない。

## Non-goals

- DB schema・index・constraint・migrationの変更。
- query最適化、eager loading、relationship追加。
- 新しいUnit of Work / service / dependency injection abstractionの追加。
- runとthread/messageを別transactionへ分割すること。
- router endpoint、response schema、frontend、Redis live updatesの変更。
- repository test全体の再編・重複排除。

## Impact estimate

### Runtime moves / additions

```text
history/citation_integrity.py -> runs/citation_integrity.py
history/mapper.py             -> runs/result_mapper.py
history/projection.py         -> threads/projection.py + runs/projection.py
history/repository.py         -> threads/repository.py + runs/repository.py
history/progress.py           -> runs/progress.py
history/types.py              -> runs/types.py
history/__init__.py           -> remove
```

新規 `contracts.py` 2本へ、repository moduleに混在しているpublic DTO / errorを移す。

### Runtime consumers

- `app/agent/router.py`: run repositoryとthread repositoryをendpointごとに使い分ける。
- `app/queue/tasks/agent_run.py`: run commandとbounded thread history readを使い分ける。
- `app/agent/question_resolution/**`: `ThreadMessageSnapshot` をthread contractからimportする。
- `app/agent/live_updates/stream.py`: run error codeをruns typesからimportする。

### Tests

- worker / repository / router testsのimportとrepository class名を更新する。
- citation integrity / progress testを `tests/agent/runs/` へ移動する。
- query条件・順序・race rollbackの既存test本文は変更しない。

### Specifications

既存specに記録された旧 `app/agent/history/*` の配置パスとclass名だけを新境界へ更新する。
historicalな保証内容・判断は変更しない。

## Risk assessment

- transaction原子性: 中。method本文と呼出側 `session.begin()` を維持し、race rollback testで検証する。
- query回帰: 中。SQLAlchemy式を変更せず移し、list/detail/history integration testで検証する。
- import cycle: 中。package facadeを作らず、`threads -> runs` の一方向依存に限定する。
- import漏れ: 中。`app.agent.history` をapp/testsから0件にする検索をDoneへ含める。
- public API / DB / Redis回帰: 低。schema・model・route・keyは変更しない。

## Work plan

1. `runs` のcontracts / types / progressを先に分離し、run型のconsumer importを更新する。
2. run projectionを切り出し、thread projectionから共通run mappingを利用する。
3. thread read projectionとrun完了用mapper / citation検査を責任別に配置する。
4. repositoryをtransaction owner単位で2classへ分け、query本文を変更せず移す。
5. router / worker / question resolution / live updates / tests / specsの参照を更新する。
6. `history` source packageを削除し、旧importが0件であることを確認する。
7. 対象unit / integration、backend標準check、全integrationを実行する。

## Done

- `app.agent.history` source packageが存在しない。
- thread read / managementは `AgentThreadRepository` だけが所有する。
- run lifecycle commandは `AgentRunRepository` だけが所有する。
- question resolution contractがrepository実装moduleに依存しない。
- transaction・query・API・DB・Redis invariantを既存testが証明する。
- app / tests と既存機能仕様に旧module path・旧repository class参照が残っていない
  （本仕様と第一段階仕様のbefore / move記録を除く）。
- backend lint / format、対象test、全integrationがgreenである。
