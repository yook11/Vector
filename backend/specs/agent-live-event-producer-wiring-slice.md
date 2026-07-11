# Agent live event producer wiring slice 仕様

更新日: 2026-07-12

実装状況: Implemented

親仕様: `agent-answer-streaming-sse.md`。

前提slice:

- `agent-live-stream-transport-slice.md`
- `agent-attempt-epoch-fencing-token-slice.md`
- `agent-sse-backend-bff-slice.md`

後続slice:

- Direct answer deltas
- Evidence answer draft deltas
- Research UI

## Positioning

本sliceは、既存のagent処理がすでに発生させている工程・検索イベントとrun終状態を、
実装済みのRedis Streamへ接続する移行sliceである。

既存経路を直ちに削除・置換するものではない。移行期間は次の責務を並行して維持する。

- Postgresの`agent_runs.progress_stage`: 低頻度で復元価値のある工程状態の正本。
- Redis Listの`recentEvents`: 既存polling UIとの互換経路。
- Redis Streamのlive event: SSEで逐次配信する短命な補助経路。
- Postgresのrun / message / source: 終状態と最終回答の唯一の正本。

本slice終了時点では、browserの`EventSource`や回答下書きUIは接続しない。またGeminiの
streaming APIへの切替と`answer.delta`の実発火も行わない。

## Work definition

### Problem

Redis Stream reader、epoch fencing、所有権確認付きSSE backend / BFFは実装済みだが、workerが
Streamへ実際に送るeventは`attempt.started`だけである。

既存agent処理は次の通知をすでに発生させている。

- `AnswerProgressReporter.stage_changed()`による`planning` / `retrieving` /
  `synthesizing`。
- `AnswerEventReporter.event_occurred()`によるagent core内の内部検索・外部検索event。
- resolver完了後にworkerが直接emitする`QuestionResolvedEvent`。
- worker / cancel APIによるrunの`completed` / `failed`遷移。

しかし、stageはPostgresだけ、activityは既存Redis Listだけへ書かれ、run終状態はRedis Streamへ
通知されていない。このままDirect answer streamingへ進むと、既存通知の移行、LLM providerの
streaming化、delta集約、最終化順序を同時に変更することになる。

本sliceではproducer接続だけを独立させ、既存通知を新Streamへ安全に二重書きし、DB commit後の
terminal通知を確立する。

### Evidence

- `backend/app/queue/tasks/agent_run.py`
  - acquire成功後に`AgentRunLiveStreamPublisher`を生成し、`begin_attempt()`だけを呼んでいる。
  - stageには`AgentRunProgressWriter`、activityには`AgentRunLiveEventPublisher`を個別に渡している。
  - `QuestionResolvedEvent`はagent構築前にworkerが生のList publisherへ直接emitしており、agentへ
    渡すreporterだけを差し替えるとStreamへの二重書きから漏れる。
  - attempt開始時に生のList publisherの`reset()`を呼び、前attemptのpolling eventを削除している。
  - 成功時は`complete_run()`、失敗時は`mark_failed()`をtransaction内で呼ぶが、commit後の
    terminal publishはない。
- `backend/app/agent/runs/progress.py`
  - stageをrunning runへbest-effortで保存する。
  - Redis、epoch、SSEを知らない。
- `backend/app/agent/live_updates/recent_events.py`
  - activityを既存Redis Listへbest-effortで保存する。
  - polling互換のため、本sliceでは削除しない。
- `backend/app/agent/live_updates/stream.py`
  - `stage`、`activity`、`terminal`の型とbest-effort publisherは実装済みである。
  - 全eventへpublisher所有の正整数`attemptEpoch`を付ける。
  - marker未確認時の`publish()`は同一epochの`attempt.started`を先頭にlazy appendする。
- `backend/app/agent/live_updates/sse.py`
  - Stream readerがdecodeしたdomain eventをSSE dataへ投影する境界で、activity固有fieldを
    snake_caseからcamelCaseへ変換している。
- `backend/app/agent/answering/orchestration.py`
  - stageの意味論を所有し、各工程の開始前にreportする。
- internal / external search実装
  - activityの意味論を所有し、`AnswerEventReporter`だけを知る。
- `backend/app/agent/runs/repository.py`
  - `complete_run()`、`mark_failed()`、`cancel_run_for_user()`は条件付きDB遷移を行う。
  - terminal publish可否は、DB遷移に勝ったかとtransaction commitが成功したかで決める必要がある。
- SSE開始契約
  - terminal entryを取りこぼしても、再接続時のDB状態と204応答で最終状態へ収束できる。
  - terminalは低遅延通知であり、正しさの唯一の根拠ではない。

### Invariants

1. Postgresをrun状態・最終回答の唯一の正本とする。
2. Stream publishの失敗・timeout・cancelを、回答生成、DB stage更新、run終状態遷移、cancel APIの
   失敗へ昇格させない。
3. agent coreはRedis、run ID、attempt epoch、SSE、HTTPを知らない。
4. stage / activityのdomain語彙は既存`AnswerProgressReporter` / `AnswerEventReporter`を正本とし、
   Stream専用の重複語彙を作らない。
5. stageは既存DB writerとStreamの両方へ通知し、一方の失敗で他方を省略しない。
6. activityは既存Redis ListとStreamの両方へ通知し、一方の失敗で他方を省略しない。
7. Redis Stream envelopeはtop-levelの`attemptEpoch`と、domain形のsnake_caseを保ったnested
   `payload.activity`を持つ。JavaScriptへ公開するSSE dataだけを
   `{ attemptEpoch, activity: camelCase AnswerProgressEvent }`へ変換する。
8. `terminal(completed)`は、assistant message、sources、run completedを含むtransactionのcommit成功後に
   だけpublishする。
9. `terminal(failed)`は、run failed遷移のcommit成功後にだけpublishする。
10. DB遷移に負けたworkerやcancel requestは、自分が成立させていないterminalをpublishしない。
11. queued runのcancelなど`attempt_epoch == 0`の終状態にはStream eventを作らない。queued待機中の
    SSEはDB再確認でterminalを検出して終了する。
12. running runのcancelは、そのrunの現在の正整数epochで`terminal(status=failed,
    errorCode=cancelled)`をpublishする。
13. 新しいpublisher instanceが同一epochの`attempt.started`を重複appendしても正当とし、consumerは
    同一epoch markerで下書きを破棄しない。
14. terminal publish後に旧workerが同epoch eventをappendし得るtransport契約は変えない。consumerが
    terminal受信後の表示更新を無視する。
15. payload、質問本文、回答本文、検索query、user IDを新しいlog / metricへ記録しない。
16. DB connectionやDB transactionを保持したままRedis I/Oを行わない。
17. 既存`recentEvents` response shape、cancel API response shape、SSE event shapeを変更しない。

### Non-goals

- Geminiの`generate_content_stream`等への切替。
- provider chunkの集約、delta coalescing、generation管理。
- `answer.delta` / `answer.reset`のproducer接続。
- evidence JSONの増分復元、citation検証、retry時のdraft reset。
- browser `EventSource`、React hook、下書きstate、画面表示。
- Redis List / `recentEvents`の削除、read停止、TTL変更。
- progress stageのDB永続化廃止。
- Redis Streamを履歴・監査ログ・最終回答の保存先にすること。
- terminal publishのexactly-once保証。
- provider処理の物理cancel。
- stale sweepやenqueue失敗に対するepoch 0のterminal event作成。
- Redis ACL、Fly proxy、CDN buffering、production負荷の運用検証。
- DB schema / migration、新規dependency、外部API response shapeの変更。

### Done

- acquire済みworkerがstageを既存DBとRedis Streamへ通知する。
- acquire済みworkerがactivityを既存Redis ListとRedis Streamへ通知する。
- workerのcompleted / failedと、running runのcancelがDB commit後にterminalをbest-effort publishする。
- DB遷移に負けた処理、epoch 0、Redis障害で誤ったterminalを作らない。
- 既存polling、最終回答保存、cancel API、SSE公開shapeが回帰しない。
- unit / worker / router / 実Redis integration testで本仕様の保証条件が固定される。
- 親仕様の次工程をDirect answer deltasへ進められる。

## Responsibility model

```text
agent core
  |-- stage_changed(stage)
  |     |-- Postgres progress writer       # 復元可能な状態
  |     `-- Redis Stream stage publisher   # 短命なライブ通知
  |
  `-- event_occurred(activity)
        |-- Redis List recentEvents writer # 既存polling互換
        `-- Redis Stream activity publisher# SSE用ライブ通知

worker / cancel API
  `-- DB terminal transition + commit
        `-- Redis Stream terminal publisher# commit後の低遅延通知
```

二重書きはmigration期間の互換性維持であり、2つの保存先を新しい正本にするものではない。

## Design

### 1. Stage reporter adapter

worker境界に、`AnswerProgressReporter`を実装するlive stage adapterを置く。

入力:

- 既存`AgentRunProgressWriter`
- acquire時に作成した`AgentRunLiveStreamPublisher`

`stage_changed(stage)`は次を独立したbest-effort sinkとして実行する。

1. 既存writerへ同じstageを渡す。
2. `AgentRunLiveStreamStageEvent(stage=stage)`をStream publisherへ渡す。

片方が例外を送出しても、もう片方を必ず試行する。fan-out adapter自身は例外をagent coreへ
伝播させない。既存sinkが持つwarning以外にpayloadを含むlogを追加しない。

stageの発火位置と順序はorchestratorが所有する。本adapterでstageを推測・追加・並べ替えしない。

### 2. Activity reporter adapter

worker境界に、`AnswerEventReporter`を実装するlive activity adapterを置く。

入力:

- 既存`AgentRunLiveEventPublisher`
- 同じ`AgentRunLiveStreamPublisher`

`event_occurred(event)`は次を独立したbest-effort sinkとして実行する。

1. 既存Redis List publisherへ元の`AnswerProgressEvent`を渡す。
2. `AgentRunLiveStreamActivityEvent(activity=event)`へ投影してStream publisherへ渡す。

adapterはactivity typeを再定義せず、既存Pydantic unionをそのままnested payloadへ入れる。
未知typeのraw dictを通すfallbackは作らない。

workerは生のList publisherに対する既存`reset()`をattempt開始時に維持し、その後にList publisherと
Stream publisherからactivity adapterを構築する。同じadapterを次の両方へ使用する。

- resolver完了後にworkerが直接emitする`QuestionResolvedEvent`。
- `build_question_answering_agent(events=...)`経由でagent coreがemitする検索activity。

これによりworker直接emitだけがList専用経路へ残る実装を禁止する。`reset()`はpolling Listだけの
lifecycle操作であり、`AnswerEventReporter`へ追加せず、Stream keyを削除しない。

Redis Stream内ではPydantic domain fieldをsnake_caseのまま保存する。例えば
`task_index` / `candidate_count`を保持し、SSE serializer境界でだけ`taskIndex` /
`candidateCount`へcamelizeする。Python modelをcamelCase化せず、ケース変換のための継承modelも
作らない。

### 3. Fan-out execution rule

stage / activityの2 sinkは互いに独立して試行する。同じ通知の待ち時間を単純加算しないため、
実装は`asyncio.gather(..., return_exceptions=True)`相当のfan-outを使用してよい。

ただし次を守る。

- 同じsinkへ同時に同一通知を二重送信しない。
- fan-out結果を理由にagent処理をraiseしない。
- event payloadや例外文字列をfan-out adapterからlogしない。
- Stream publisherの0.5秒per-operation timeoutを変更しない。
- stage間、activity間のglobal orderingを新たに保証しない。Redis Stream ID順はappend順だけを表す。

外部検索taskは並列にactivityを発生させ得るため、task間の並びは非決定的である。各eventの
domain fieldである`task_index`を帰属に使い、SSE公開時だけ`taskIndex`へ変換する。意味論を
Stream IDの隣接関係へ依存させない。

### 4. Worker terminal publication

workerはacquire時に作成した同一`AgentRunLiveStreamPublisher`を、attempt開始から終状態通知まで
保持する。

#### Completed

1. `agent.answer()`が`AnswerQuestionResult`を返す。
2. `complete_run()`をDB transaction内で実行する。
3. transaction contextを正常終了し、commit成功を確認する。
4. DB遷移に勝った場合だけ`terminal(status=completed, errorCode=null)`をpublishする。

`complete_run()`がfalse、`RunTransitionLostError`、commit失敗のいずれかならcompleted terminalを
publishしない。Redis publish失敗でも、すでにcommit済みのrunをfailedへ戻さない。

#### Failed

1. workerが既存のerror codeを決める。
2. `mark_failed()`をDB transaction内で実行する。
3. transaction commit成功を確認する。
4. failed遷移に勝った場合だけ`terminal(status=failed, errorCode=<既存code>)`をpublishする。

`_mark_failed()`相当のworker helperを、generation unavailable、unexpected error、completion
transaction失敗の全呼び出し元が通る単一のchoke pointにする。helperは遷移に勝ったかをcallerへ
返せる内部契約を持ち、commit成功後のfailed terminal試行までを同じ規則で行う。DB失敗とRedis失敗を
同じ結果値へ潰さない。

### 5. Cancel terminal publication

cancel APIのHTTP statusと認証・認可契約は変更しない。

repositoryは、cancel遷移に勝った場合に内部情報としてそのrunの`attempt_epoch`をcallerへ返せる
契約にする。外部responseへepochを追加しない。

terminalに使用するepochは、cancel前のSELECTやORM instanceから取得しない。active statusを条件に
したcancelの`UPDATE`と同じstatementへ`RETURNING attempt_epoch`を付け、実際に遷移させたrowのepochを
原子的に取得する。初期SELECTは所有権と既存terminal状態の分類に使用してよいが、そのepochをpublishへ
使用しない。UPDATEが0行なら既存どおり再読してnot found / already completed / already failedへ収束する。

- `attempt_epoch >= 1`: transaction commit後に新しいStream publisherを作り、
  `terminal(status=failed, errorCode=cancelled)`をbest-effort publishする。
- `attempt_epoch == 0`: Stream envelopeの正整数契約に従いpublishしない。
- already completed / already failed / not found: terminalをpublishしない。
- terminal publish失敗: cancel成功の204を変更しない。

cancel経路のpublisherはmarker成功状態を共有していないため、同一epochの`attempt.started`をterminalの
直前に重複appendし得る。これはtransportのlazy marker契約に準拠し、同一epochなのでdraft破棄境界に
ならない。

Redis I/OはDB transaction終了後に行う。publishのためにDB sessionを開いたままにしない。

### 6. Terminal gaps accepted by this slice

terminal eventは正しさの正本ではなく低遅延通知であるため、次の経路ではterminalが無いことを
受容する。

- queuedのままcancel / enqueue failureとなり、epochが0のrun。
- stale sweepがworker外でfailedへ遷移させたrun。
- DB commit後にRedis publishが失敗・timeoutしたrun。
- process crashがDB commitとpublishの間に発生したrun。

これらはSSEのmax age、再接続preflight、run polling、DB terminal状態により最終結果へ収束する。
本sliceでoutboxやDB-backed event deliveryを追加しない。

### 7. Attempt and race rules

- すべてのworker stage / activity / terminalはacquire時の同じ整数epochを持つ。
- 新attempt開始後に旧workerがpublishした小さいepoch eventはreader fencingで除外される。
- cancelとcompleteが競合した場合、DB条件付き遷移に勝った側だけがterminalをpublishする。
- terminal publish後の同epoch eventをtransport側で削除・filterしない。
- `begin_attempt()`失敗後の最初のstage / activity / terminal publishは、publisherのlazy retryにより
  同じpipeline先頭へmarkerを追加する。

### 8. Failure and observability

新adapterの失敗は既存publisherの低cardinality warningへ委譲する。

許可する診断属性:

- 固定event名
- `run_id`（既存worker log規約の範囲）
- event type
- operation

禁止する属性:

- activity payload
- standalone question / query
- answer本文
- Redis entry / envelope本文
- Redis例外文字列
- user ID

本sliceでは新しいmetricを必須にしない。既存SSE側のevent配信数・close reasonとpublisher warningで
障害を観測する。producer drop率のmetricが必要になった場合はpayloadを含まないevent type labelだけを
別sliceで追加する。

## Required file changes

実装時の想定変更範囲を次に固定する。実態確認で責務が異なる場合は、scopeを広げる前に仕様を更新する。

```text
backend/app/agent/live_updates/reporters.py
  # stage / activity fan-out adapters

backend/app/queue/tasks/agent_run.py
  # adapter配線、commit後のcompleted / failed terminal publish

backend/app/agent/router.py
  # cancel commit後のbest-effort terminal publish

backend/app/agent/runs/contracts.py
backend/app/agent/runs/repository.py
  # cancel成功時のattempt epochを返す内部契約（外部API shapeは不変）

backend/tests/agent/live_updates/test_reporters.py
backend/tests/agent/test_agent_run_task.py
backend/tests/agent/test_agent_router.py または既存router test
backend/tests/agent/live_updates/*integration*
  # 本仕様のunit / worker / router / 実Redis保証

backend/specs/agent-answer-streaming-sse.md
  # slice順序と完了状況の同期
```

`app.agent.contract`の既存Protocolとevent union、Stream eventの公開shapeは変更しない。変更が必要に
なった場合は本sliceを停止し、API / transport契約変更として再判断する。

## Tests

### Unit: stage reporter

1. `planning` / `retrieving` / `synthesizing`を、DB writerとStream publisherの両方へ1回ずつ渡す。
2. DB writerがraiseしてもStream publishを試行し、agent coreへraiseしない。
3. Stream publisherがraiseまたは`None`を返してもDB writerを試行し、agent coreへraiseしない。
4. Stream eventが元stageを変更せず、同じpublisher epochでencodeされる。

### Unit: activity reporter

1. 各既知`AnswerProgressEvent`を既存List publisherとStream publisherの両方へ1回ずつ渡す。
2. List publisher失敗時もStream publishを試行する。
3. Stream publisher失敗時もList publishを試行する。
4. activityを`AgentRunLiveStreamActivityEvent(activity=event)`へnested投影し、flattenや`event` fieldを
   作らない。
5. adapterのlog / exceptionへpayload本文を含めない。

### Worker lifecycle

1. acquire成功時だけreporterとStream publisherを構築する。acquireが`None`ならbegin / stage /
   activity / terminalを発火しない。
2. attempt開始時の既存List `reset()`を維持し、Stream keyを削除しない。
3. standalone questionが元質問と異なる場合、worker直接emitの`QuestionResolvedEvent`が既存Listと
   Streamの両方へ届く。
4. `begin_attempt()`失敗後の最初のstageまたはquestion resolved activity publishでmarker lazy retryが
   働く。
5. answer成功、`complete_run()`成功、commit成功の順序後にcompleted terminalを1回試行する。
6. `complete_run()`がfalseまたは`RunTransitionLostError`ならcompleted terminalを送らない。
7. completion transactionがrollback / commit失敗したらcompleted terminalを送らない。
8. generation failureで`mark_failed()`とcommitが成功した後に、同じerror codeのfailed terminalを
   1回試行する。
9. unexpected errorとcompletion transaction失敗も同じ`_mark_failed()` choke pointを通り、
   `mark_failed()`とcommit成功後に対応するfailed terminalを試行する。
10. failed遷移に負けた場合は、どの呼び出し元でもfailed terminalを送らない。
11. terminal publishが失敗してもworkerがDB terminal状態を変更し直さない。
12. stage / activityのStream失敗が`agent.answer()`と最終DB保存を妨げない。

### Cancel route

1. 所有者のrunning runをcancelし、commit後に現在epochの`failed + cancelled` terminalを試行する。
2. cancel成功時のepochは同じ条件付き`UPDATE ... RETURNING attempt_epoch`から取得し、事前SELECTの
   epochを使用しない。
3. SELECT時にqueued epoch 0だったrunがcancel UPDATE前にacquireされepoch 1になったraceでも、
   `RETURNING`したepoch 1でterminalを送る。
4. queued epoch 0のままcancelした場合は204を返し、Stream publisherを作らない。
5. not found、他者所有、already completed、already failedではterminalを送らず、既存404 / 409 / 204
   契約を維持する。
6. cancel terminal publish失敗でも成功済みcancelの204を維持する。
7. Redis操作はcancel DB transaction終了後に始まる。

### Race and fencing

1. epoch 2開始後にepoch 1 workerがstage / activity / terminalをpublishしても、epoch 2 reader結果へ
   混ざらない。
2. cancelとcompleteの競合で、DB遷移に勝った側のterminalだけが作られる。
3. cancel経路が同一epoch markerを重複appendしても、reader結果のepoch帰属が変わらない。
4. terminal後の同epoch activityはtransportに残り得ることを固定し、consumer責務を変えない。

### Real Redis integration

1. `attempt.started -> stage -> activity -> terminal`が同じepochでStream ID昇順に読める。
2. Redis Stream entryの`payload.activity`はnestedかつdomain形のsnake_case
   (`task_index` / `candidate_count`等)で保存される。
3. 同じentryをSSE serializerへ渡すと、公開dataだけがnested camelCase
   (`taskIndex` / `candidateCount`等)になる。
4. 既存Redis Listにも同じdomain activityが残り、`recentEvents` pollingが回帰しない。
5. Stream keyのMAXLEN / TTL / timeout契約を変更しない。
6. testは所有するUUID keyだけを削除し、共有Redisへ`FLUSHDB`しない。

### Regression

1. direct / evidenceの最終回答、sources、missing aspectsのDB保存結果が変わらない。
2. progress stage pollingが従来どおりDBから復元できる。
3. `GET /runs/{runId}`の`recentEvents` shapeが変わらない。
4. cancel endpointのOpenAPI response shapeが変わらない。
5. Stream / SSE event vocabularyへ新typeを追加していない。

## Implementation order

1. 本仕様のunit test表から、stage / activity fan-outの失敗分離testを先に追加する。
2. reporter adaptersを実装し、既存List `reset()`を維持したうえでworker直接emitとagent compositionの
   両方へ同じactivity adapterを接続する。
3. worker completed / failedのDB commit後terminal testを追加して実装する。
4. cancel outcomeの内部contract testを追加し、`UPDATE ... RETURNING`でepoch 0 / positive epochと
   acquire競合を分ける。
5. cancel routeのcommit後terminalを実装する。
6. 実Redisで二重書き、epoch、event順序、既存List互換を検証する。
7. backend全checkと関連integration testを実行する。
8. 親仕様のDone / slice進行状況を更新する。

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| 二重書きの片方だけ失敗 | pollingとSSEで一時表示が異なる | 両方best-effortかつ独立試行。DB最終状態を正本にする |
| Redis障害が検索を遅延 | 回答生成時間が伸びる | 既存0.5秒timeoutを維持し、fan-out待ちを単純加算しない |
| DB commit前terminal | 未確定状態をclientが確定扱いする | transaction context終了後だけpublishするtestを必須化 |
| cancel / complete競合 | 矛盾するterminalが2つ届く | DB条件付き遷移の勝者だけpublishする |
| queued cancelでepoch 0 publish | envelope契約違反 | epoch 0はpublishせずDB再確認へ収束 |
| 新publisherによるmarker重複 | client draftの誤破棄 | 同一epoch marker重複を正当化し、epoch増加だけを破棄条件にする |
| terminal publish喪失 | SSEが完了を即時検知できない | max age、204 preflight、pollingでDB終状態へ収束 |
| adapterがdomain責務を持つ | 語彙の二重管理 | adapterは既存eventを機械的にStream型へ投影するだけに限定 |

## Acceptance summary

本sliceは「既存のライブ情報を新しい配信経路へ移す」段階だが、即時置換ではない。

- stageの正本はPostgresのまま。
- polling互換activityは既存Redis Listへ残す。
- 同じstage / activityをRedis Streamにも送り、SSEで読めるようにする。
- terminalはDB commit後の通知として追加する。
- Redis Streamが壊れても既存回答生成・polling・最終DB結果は壊れない。

この責任境界を満たした後、次sliceでplain textのDirect answer delta生成へ進む。
