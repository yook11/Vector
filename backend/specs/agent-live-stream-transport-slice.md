# Agent run Redis Stream 配信基盤 slice 仕様

## 位置付け

親仕様: `agent-answer-streaming-sse.md`。

本 slice は、回答下書き・進捗・terminal event を将来 SSE で届けるための、
**Redis Stream の保存・再開基盤だけ**を作る。HTTP endpoint、Next.js BFF、
browser、Gemini のstreaming化は後続sliceの責務である。

既存の `agent-run-live-events-slice.md` が作った Redis List と `recentEvents` は
変更しない。両者は並行して存在し、既存 polling の表示契約は不変とする。

## Problem

Redis List による `recentEvents` は、最新数件を polling 応答へ載せる用途には適するが、
イベントIDを持たず、接続断後に「どこから再開するか」を表せない。回答下書きを
SSEで配信するには、runごとに順序付きIDを持ち、readerごとに同じイベントを再取得
できる短命なログが必要である。

同時に、このログは最終回答や会話履歴の保存先になってはならない。Redisの遅延・
断・ACL設定不足が、workerの回答生成やPostgresのrun状態を壊さない設計が必要である。

## Evidence

- 現行の `AgentRunLiveEventPublisher` は `LPUSH` / `LTRIM` / `EXPIRE` で検索中の
  表示イベントを保持し、失敗を握りつぶす best-effort 実装である。
- 既存の `reset()` は Redis List key を `DEL` する。Streamで同じ方法を使うと、
  browser が保持する再接続カーソルとの連続性を失う。
- `redis>=7.4.0,<8` と `redis.asyncio` は導入済みであり、実Redisを使う integration
  test の土台も存在する。
- Redis Stream の `XADD` は時間順のIDを発行し、`XREAD` は指定IDより後のentryを
  readerごとに返す。これはconsumer groupを使わない複数browserへのfan-outと一致する。
- `acquire_for_execution()` は queued / running のrunを再取得し、そのたびに
  `agent_runs.started_at` を更新する。worker timeoutは300秒であり、重複配送時には
  旧workerが新attemptの開始後に遅延eventをpublishし得る。
- 現在の共有Redis clientには、長時間blocking read用の専用pool・timeout設定がない。
  このsliceで `XREAD BLOCK` を使うと、将来のSSE接続が共有poolを占有し得る。

## Scope

### In scope

1. run単位のRedis Stream key、保持期間、件数上限、短時間timeoutを定義する。
2. `PreparedAgentRun` が持つ `attempt_epoch`（取得時にDBへ保存した `started_at`）を、
   すべてのStream envelopeに載せる。
3. `attempt.started`、`stage`、`activity`、`answer.delta`、`answer.reset`、`terminal`
   の内部event vocabularyと、共通envelopeとevent payloadを分けた厳格なdecodeを定義する。
4. worker側のbest-effort publisherと、SSE層が利用するnon-blocking readerを実装する。
5. runの実行取得後に `attempt.started` をpublishし、marker失敗時は同一epochでlazy retry
   してから次eventをappendするlifecycleを確立する。
6. 実Redisで順序、再開、epoch境界、上限、TTL、障害時の劣化を検証する。
7. 本番Redis ACLに必要なcommand・key patternをdeploy前チェック項目として明記する。

### Out of scope

- FastAPIのSSE endpoint、認可、`Last-Event-ID` HTTP header、heartbeat、BFF proxy。
- `XREAD BLOCK`、長時間Redis接続、SSEの接続数制御。
- `EventSource`、React state、下書きの表示、polling fallbackのfrontend実装。
- Geminiを `generate_content_stream` へ切り替えること、および `answer.delta` の実発火。
- evidence JSONの増分復元、citation検証retry時の `answer.reset` の実発火。
- 既存 `recentEvents` / Redis List の移行、削除、response schema変更、型生成。
- DB schema migration、回答下書きのDB保存、consumer group、consumer ack。

## Storage contract

### Key and retention

```text
key:     agent:run:{run_id}:live
type:    Redis Stream
TTL:     900 seconds (15 minutes)
MAXLEN:  4096 entries, exact
page:    128 entries per cursor read
timeout: 0.5 seconds per logical publish / read
```

- `MAXLEN=4096` は、現行の回答生成上限が1回あたり2048 output tokensであることと、
  lifecycle / activity event の余白を合わせた初期上限である。将来 output token上限を
  上げる場合は、この値とdelta coalescing方針を同じsliceで見直す。
- exact trimを使う。このStreamは短命のユーザー本文を含み得るため、「おおむね上限」では
  なく明示的な件数上限を契約にする。
- publisher はentry追加ごとにTTLを更新する。runが止まれば最大15分で自然に消える。
- 4096件より古いcursor、またはTTL切れのcursorから完全な下書きを復元する保証はない。
  その場合のUIは後続sliceでthread詳細とpollingへ劣化させる。
- publisherのwriteとreaderの1回の論理readは、それぞれ全体を`asyncio.wait_for`で
  0.5秒に制限する。複数Redis commandが必要でも、commandごとに0.5秒を積み上げない。
- cursorなしの初回readは、上限4096件を一度だけ`XRANGE`してepochで絞り込む。cursorが
  あるreadは`XREAD COUNT 128`だけを使う。SSE層は追いつくまで短いnon-blocking readを
  繰り返し、その後に初めてblocking readへ移る。

### Envelope

Redis entryは文字列fieldだけで構成し、payloadはUTF-8 JSONにする。

```text
type:       event type
attemptEpoch: `PreparedAgentRun.attempt_epoch` をISO 8601でserializeした値
payload:    JSON object
publishedAt: publisherが付与する UTC ISO 8601 timestamp
```

readerは共通envelope（type / attemptEpoch / publishedAt）を先に検証し、その後にevent別の
payloadをPydantic discriminated unionで検証する。未知のtype、壊れたJSON、schema違反の
entryは個別に捨て、同じStream内の有効entryを返す。`attempt.started`のpayloadだけが壊れても
共通envelopeのepochが有効なら、他entryのepoch境界判定を壊さない。decode失敗のpayloadは
ログへ出さない。

### Event vocabulary

以下の `{ ... }` はSSEへ出す平坦なevent表記である。Redis entryでは `attemptEpoch` は
共通envelope field、残りは `payload` JSONに入れる。

```text
attempt.started
  { attemptEpoch }

stage
  { attemptEpoch, stage: "planning" | "retrieving" | "synthesizing" }

activity
  { attemptEpoch, event: existing safe AnswerProgressEvent }

answer.delta
  { attemptEpoch, generation: positive integer, text: non-empty string }

answer.reset
  { attemptEpoch, generation: positive integer }

terminal
  { attemptEpoch, status: "completed" | "failed", errorCode?: existing run error code }
```

- `attempt.started` は、workerが `acquire_for_execution()` に成功した後、agent構築前に
  publishする。再配送による新attemptには、新しい `attemptEpoch` が付く。
- publisherがmarkerの成功を確認できない場合、次の`publish()`は同一epochの
  `attempt.started` を先頭にappendする。timeout後に最初のappendが成功していた場合も
  marker重複は許容する。
- markerはUIの破棄通知であり、event帰属の正本ではない。readerは常にenvelopeの
  `attemptEpoch` が要求epochと一致するentryだけを返す。
- Stream keyは削除しない。Stream IDは単調な再接続cursorとしてのみ使う。
- `answer.delta` / `answer.reset` / `terminal` はこのsliceで型とstorageだけを用意する。
  producerを接続するのはそれぞれ後続sliceである。

## Design

### Publisher

`AgentRunLiveStreamPublisher` は worker側に置く。

- `PreparedAgentRun.attempt_epoch` をconstructorで受け、すべてのenvelopeへ同じ値を入れる。
- `begin_attempt()` は `attempt.started` をappendし、成功確認済みかをpublisher内部に保持する。
- `begin_attempt()` がtimeout・失敗ならrunをfailedにせず、成功確認済みになるまで次の
  `publish(event)` のpipeline先頭に同一epochの `attempt.started` を挟む。
- `publish(event)` はeventをJSON化し、`XADD MAXLEN 4096` と `EXPIRE 900` を短い
  pipelineで実行する。同一epochのmarker重複は正当なlazy retryである。
- 成功時はRedis Stream IDを返す。失敗・timeout時は `None` を返し、例外をagent coreへ
  伝播しない。
- `asyncio.wait_for` はpipeline全体を囲む。timeoutはRedisが操作を実行しなかった証明では
  ないため、retry後の重複を前提にする。
- warning logには run ID、event type、操作種別だけを残す。payload、Redis例外文、
  回答本文、質問本文を残さない。
- `begin_attempt()` の失敗も同じbest-effortであり、runをfailedにしない。

publisherは `AnswerProgressReporter` / `AnswerEventReporter` と別のtransport adapterである。
agent coreはRedisやrun IDを知らず、後続sliceが必要なreporterをconstructor injectionで
接続する。

### Reader

`AgentRunLiveStreamReader` は、SSE endpointが後から使う読み出し部品である。

- `read_after(run_id, attempt_epoch, cursor)` は要求epochを必須入力とする。SSE endpointは
  所有権確認で得た現在のrunの `started_at` を渡す。
- cursorなしの初回readは`XRANGE`で最大4096entryを一度だけ読む。cursorありのreadは
  `XREAD COUNT 128`を**blockなし**で呼ぶ。
- readerは要求epochと一致する有効entryだけをID昇順で返す。新attempt開始後に旧workerが
  遅延publishしたentry、markerより後に届いた旧epoch entryも返さない。
- `next_cursor` は返却eventの末尾ではなく、今回消費したraw entryの末尾IDとする。旧epoch
  entryだけを読んだ場合もcursorを進め、同じentryを無限に読み直さない。
- cursorありのreadでは、最古の残存Stream IDを`XRANGE COUNT 1`で確認する。cursorが
  それより古ければ `cursor_trimmed` とし、不完全な下書きの継続を許可しない。
- Stream IDは`<milliseconds>-<sequence>`を数値pairとして比較する。文字列の辞書順比較は
  しない。
- readerの結果型は次を区別する。

  ```text
  events          # 要求epochの有効eventを返す
  empty           # 既に同epochを読んだcursor以後に新規eventがない
  stream_missing  # keyが存在しない（TTL切れ・未作成）
  attempt_absent  # keyはあるが、初回readで要求epochの有効entryが1件もない
  cursor_trimmed  # cursorより新しい一部entryがtrim済みで完全再開できない
  unavailable     # Redis例外・timeout
  ```

  `stream_missing`、`attempt_absent`、`cursor_trimmed`、`unavailable` は後続SSE層が
  接続を閉じ、pollingと最終DB結果へ劣化する信号である。
- `read_after()` が内部で行う`XRANGE` / `XREAD` / `EXISTS`を含む全readは、1回の
  `asyncio.wait_for`で0.5秒以内に収束させる。
- reader自身はrun所有権を確認しない。これはHTTP/API境界の責務であり、後続SSE endpointは
  必ずDBで所有権確認してからreaderを呼ぶ。
- readerはconsumer groupを使わない。各browser connectionが同じeventを独立に読める
  ことが要件である。
- readerはterminal後の同epoch entryも素通しする。terminal後の表示停止はconsumerの責務で
  あり、このtransport層はeventを再解釈しない。

### Attempt boundary

既存Redis Listのように `DEL` で履歴を消さない。削除後に生成されるStream IDと、
browserが持つ古いcursorの大小関係を保証できないためである。

attemptの帰属はappend順序ではなくepochだけで決める。したがって、最新attemptのmarkerが
trim・破損・lazy retry重複しても、残存entryの `attemptEpoch` が要求epochと一致すれば
返せる。逆に、新attemptのmarker後に旧workerがappendした旧epoch entryは返さない。

read完了直後に新attemptが始まるraceは避けられない。readerの保証は「read開始時に渡された
epochに一致するentryだけを返す」までである。consumerは異なるepochの`attempt.started`を
受信した時点で下書きを破棄し、同一epochの重複markerでは何もしない。

## Required file changes

```text
backend/app/agent/live_updates/stream.py          # publisher / reader / event models / constants
backend/app/agent/live_updates/__init__.py        # live transport package boundary
backend/app/agent/runs/contracts.py               # PreparedAgentRun.attempt_epoch
backend/app/agent/runs/repository.py              # acquire時にattempt_epochを返す
backend/app/queue/tasks/agent_run.py              # acquire成功後の begin_attempt() 接続のみ
backend/tests/agent/live_updates/test_stream.py   # unit + real Redis integration
backend/tests/agent/test_agent_run_task.py        # worker正負パス + attempt epoch回帰
```

`app/schemas/research.py`、generated TypeScript、frontend、router、既存
`recent_events.py` は変更しない。既存のworker task・repositoryに未コミット変更がある場合は、
実装開始前に差分の所有者と競合範囲を確認する。

## Redis ACL and deployment check

本番導入前に、worker-agent と API が使うRedis ACL userを分けて以下を確認する。

```text
key pattern: ~agent:run:*

worker-agent: +xadd +expire
API:          +xread +xrange +exists
```

`XREAD BLOCK` はこのsliceに含めないため、blocking接続数やidle timeoutはSSE/BFF sliceで
改めて検証する。既存List keyの `agent:run:{id}:events` と同じkey prefixを使うため、
新規に確認・開放する中心はStream commandである。ACL不足はlive表示の障害に留めず、
deploy前に検出・修正する。

## Invariants

- Postgresが会話・run状態・最終回答の唯一の正本である。
- Redis Streamのwrite / read / decode / timeout失敗は、回答生成、DB transaction、
  run状態遷移を失敗させない。
- Stream IDはpublish順序とreaderの再開cursorにだけ使い、DBへ保存しない。
- 同じeventを複数readerが読むことを許容する。exactly-once配信は保証しない。
- 同じcursorからのreadは重複を返し得る。後続clientはIDでdeduplicateする。
- event帰属はenvelopeの `attemptEpoch` の等値比較だけで決める。marker位置やappend順序では
  決めない。
- 新attemptのmarker後に旧workerが遅延publishしても、epoch不一致のentryはreaderが返さない。
- 同一epochの `attempt.started` 重複は、readerのevent集合・consumerの下書き破棄境界を
  変えない。
- readerの保証はread開始時に渡されたepochまでである。read後の再配送はbatchを遡って
  無効化せず、consumerがepoch変化を境界として扱う。
- readerはnon-blockingであり、共有Redis poolを長時間占有しない。
- trim済みcursorから完全な下書きの再構成はしない。readerは `cursor_trimmed` を返し、
  consumerは下書きを継続表示しない。
- Stream payloadに質問本文、prompt、chain of thought、未選別evidence、provider生応答、
  secret、例外本文を含めない。
- payloadはログ、metric label、trace attribute、例外messageへ含めない。
- 既存List / `recentEvents` の動作・API契約・polling間隔は不変である。

## Tests

各invariantには、対応するtestを少なくとも1つ置く。既存の
`live_updates/test_recent_events.py` はRedis List向けのTTL・timeout・payload非漏洩の流儀を
示すだけであり、以下のRedis Stream保証を代替しない。

### Unit

1. key名、定数、共通envelope、全event typeのencode/decodeを検証する。すべてのentryに
   `attemptEpoch` が入り、要求epochと一致するentryだけが返る。
2. 未知type、壊れたJSON、event payloadのschema違反を個別skipする。`attempt.started`の
   payloadが壊れても共通envelopeが有効なら、epoch境界判定が他entryで成立する。
3. `begin_attempt()` がtimeoutした後の次publishは、同一epochのmarkerを先頭に再送する。
   timeoutが実際にはRedisで成功していた場合に同一epochのmarkerが重複しても、boundary
   判定が変わらない。
4. readerの結果型が `events`、`empty`、`stream_missing`、`attempt_absent`、
   `cursor_trimmed`、`unavailable` を区別する。
5. Stream IDを数値pairとして比較し、`1-9` と `1-10`、`9-0` と `10-0` を辞書順で
   誤判定しない。
6. logical publish / readの全体が0.5秒以内にtimeoutする。複数の内部Redis操作が順に
   遅延しても、operationごとに0.5秒を積み上げない。
7. publisherの例外・timeoutが`None`に収束し、payloadとRedis例外本文がlogに出ない。

### Repository and worker integration

1. `acquire_for_execution()` がrunning runを再取得したとき、DBの `started_at` が新しい値へ
   更新され、返す `PreparedAgentRun.attempt_epoch` がその値と一致する。terminal runは
   `None` のままである。
2. acquire成功時だけworkerが `AgentRunLiveStreamPublisher.begin_attempt()` をagent構築前に
   呼ぶ。idempotent skip / terminal runではpublisher生成、begin、publishのいずれも起きない。
3. `begin_attempt()` が失敗してもworkerは回答を続行し、runの成功・失敗状態を変えない。

### Real Redis integration

1. `XADD` IDは昇順であり、同じcursorを使う複数readerが同じevent列を読む。consumer groupを
   作らない。
2. epoch2の `attempt.started` 後にゾンビworkerがepoch1のeventをappendしても、epoch2を
   要求したreaderはepoch1 eventを返さない。
3. 同一epochの `attempt.started` が2回あっても、そのepochのevent集合は不変であり、
   新attemptとして扱われない。
4. 最新attemptのmarkerをtrimしても、残った同一epoch eventは返る。marker位置に依存した
   境界判定をしていないことを確認する。
5. cursorがtrim済み領域を指すときは `cursor_trimmed` を返す。残存entryを完全な下書きと
   偽って継続しない。
6. 4096件を超えてもStream長が上限以下であり、publishでTTLが更新される。128件を超える
   同一epoch replayはpage単位で順序を保ち、重複なく読める。
7. terminal後に同一epochのentryをappendしてもreaderは素通しする。terminal以後の表示停止は
   後続SSE / UI sliceで検証する。
8. Redisを停止・遅延させてもpublisher / readerが総timeout内に復帰し、run処理へ例外を
   伝播しない。

## Verification

- backendの対象テスト、`live_updates/test_recent_events.py`、repository / worker回帰テストを実行する。
- 実装変更後に `/check` を実行する。
- deploy前にRedis ACLのcommand / key patternを実環境で確認する。
- migration、`/gen-types`、frontend検証はこのsliceでは不要である。

## Done

- run単位のRedis Streamへ、型付きeventを上限・TTL付きでbest-effort appendできる。
- readerがRedis Stream IDをcursorにして、要求epochと一致する有効eventだけを順序通り取得
  できる。ゾンビworkerの遅延publish、marker重複・破損・trimで旧attemptを混ぜない。
- Redis障害、壊れたentry、未知event、trim済みcursor、worker再配送で最終回答・run状態・
  既存pollingが壊れず、不完全な下書きは劣化表示へ移る。
- 実Redisテストとtimeout / payload非漏洩テストがgreenである。
- SSE endpoint、UI、Gemini streaming、既存 `recentEvents` へ変更を入れていない。
