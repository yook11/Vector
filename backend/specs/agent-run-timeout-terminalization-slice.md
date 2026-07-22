# Agent run timeout terminalization slice 仕様

Status: Implemented

Date: 2026-07-22

## 位置付け

Vector の Research run は、Postgresの `agent_runs` を永続状態の正本とし、Taskiqで非同期実行し、
Redis Streamのlive eventとDB pollingで画面へ進捗を伝える。

本sliceは、Taskiqの外側timeoutによってhandlerがcancelされたとき、Taskiq messageはACKされ得る一方で、
DBのrunが `running` のまま残り、ユーザー画面が長時間「生成中」から変わらない問題を解消する。
同時に、受付から3分を超えたqueued runの遅延実行を禁止し、未配送queued runを5分で回収する。

global run capacity、Research専用Redis Streamの分離・配送・pending回収設計は、本sliceに含めない。
`backend/specs/agent-global-run-capacity-slice.md` は保留とし、このtimeout問題については本仕様を正本とする。

本sliceは次の既存仕様へ追加する。

- `specs/agent/conversation-history-async-runs.md`
- `backend/specs/agent-history-run-execution-slice.md`
- `backend/specs/agent-attempt-epoch-fencing-token-slice.md`
- `backend/specs/agent-live-stream-transport-slice.md`
- `backend/specs/agent-user-daily-request-quota-slice.md`

## Work Definition

### Problem

現行の `run_agent_answer` はTaskiq task timeoutを300秒に設定している。Taskiq 0.12.4のreceiverは、
handler全体をtimeoutでcancelし、その例外をtask resultへ変換した後、`when_executed` のmessageを
ACKできる。handlerの通常の `except Exception` と `failed` commitへ到達できなければ、次が同時に起こる。

- Redis messageはACKされ、同じmessageの自動再配送を期待できない。
- Postgresのrunは `running` のまま残る。
- terminal eventは発行されない。
- frontendはDB pollingでもterminal状態を取得できず、「回答を生成中」の表示を続ける。
- 現行の20分stale閾値・10分間隔sweepでは、回収が概ね20〜30分後になる。

ユーザーがResearch requestを送信した後、3分を超えて通常の生成中表示を続けることは、
本アプリのUXとして許容しない。

また、3分を超えてqueuedのまま残ったrunが後からworkerへ配送され、新しいprovider callを開始することも
許容しない。現行の20分queued回収では、画面が待機を終了した後もrunと日次quota予約が長時間残る。

### Evidence

2026-07-22時点のrepositoryとworkspace runtimeを根拠とする。

- `backend/app/queue/tasks/agent_run.py::run_agent_answer()`
  - `timeout=300`、`max_retries=0`、`retry_on_error=False` である。
  - run取得後にDBを `running` へcommitする。
  - 通常のprovider errorと `Exception` は `failed` へ遷移させる。
  - outer cancellationはこの例外処理とterminal commitを完了できない可能性がある。
- workspaceのTaskiqは0.12.4である。
  - receiverはhandlerを `anyio.fail_after(timeout)` の内側で実行する。
  - handlerの `BaseException` をtask resultへ変換する。
  - `when_executed` はtask resultがerrorでも、実行終了後にACKする。
- `backend/supervisord/agent.conf` は `--ack-type when_executed` でworkerを起動する。
- `backend/app/agent/runs/repository.py::acquire_for_execution()` は、取得成功時に
  `status='running'`、`started_at=now`、`attempt_epoch + 1` を同じtransactionでcommitする。
- `backend/app/agent/runs/repository.py::mark_failed()` は、active statusと
  `expected_attempt_epoch` の一致を条件にterminal化できる。
- `backend/app/agent/runs/repository.py::sweep_stale_runs()` は、現在queued/runningを同じ20分閾値で
  `failed/stale` にする。
- `backend/app/queue/schedule.py::CRON_AGENT_RUN_SWEEP` は10分間隔である。
- `backend/app/queue/tasks/agent_run.py::_publish_terminal()` は、DB terminal commit後にだけ呼ばれる
  best-effort publisherである。
- frontendはSSEと2秒間隔のDB pollingを併用し、DB terminal状態を最終的な正本として表示する。
- `ResearchUserMessage.createdAt` は既にfrontendへ公開されており、request受付時刻に近い
  server-side timestampとして利用できる。API response shapeの追加は不要である。

既存実装は変更対象を特定する証拠であり、本仕様の正しさそのものではない。

### Invariants

1. DBのrun statusが永続状態の正本であり、Redis eventとfrontend local stateは正本にしない。
2. timeoutを検知しただけでは `failed` をユーザーへ確定表示しない。DBのterminal commit成功を必要とする。
3. terminal eventはDB terminal commit後にだけbest-effortで発行する。
4. terminal eventの発行失敗は、commit済みのDB terminal状態をrollbackまたはactiveへ戻さない。
5. application timeoutのterminal遷移は `attempt_epoch` でfenceし、古いattemptが新しいattemptを終了させない。
6. completed、policy_blocked、failedのterminal runをtimeoutまたはsweeperが上書きしない。
7. 画面はrequest受付から180秒後に通常の「生成中」表示を終了する。
8. 180秒時点でDBがactiveなら、画面は失敗確定ではなく「状態更新中」を表示し、pollingを継続する。
9. frontendはtimeout後にrequestを自動再送しない。
10. 受付から180秒を超えたqueued runは `running` へ遷移させず、providerを呼ばない。
11. 未配送のqueued runは受付から5分超過で回収する。
12. quotaを返却できるのは、queuedからterminalへの条件付き遷移に成功したcancelまたは期限切れ経路だけである。
    本sliceは期限切れ経路を返却対象へ追加する。
13. 一度 `running` へ遷移したrunのtimeoutまたはstaleでは日次quotaを返却しない。
14. timeout、terminal化、sweepのlogとmetricへ質問本文、回答本文、user ID、provider raw responseを含めない。

### Non-goals

- global queued数またはqueued + running数によるadmission制御。
- per-user active run上限の追加または変更。
- Redis Streamの分離、queue depth判定、pending回収、dead-letter queueの再設計。
- Taskiq workerのprocess数、`--max-async-tasks`、broker topologyの変更。
- provider call、token、検索fan-out単位の予算制御。
- ユーザー日次quota上限、予約時点、同一thread active run制約の変更。
- IP単位の不正request検知。
- provider SDK固有timeoutの統一。
- production deploy、service再稼働、image固定。
- 新しいDB column、table、index、Alembic migration。
- 新しいdependencyの追加。
- frontend公開APIのstatus、error code、response shapeの変更。

### Done

次をすべて満たした時点で本sliceを完了とする。

1. answer generationに150秒のapplication timeoutが適用される。
2. Taskiqの外側timeoutが180秒になる。
3. application timeout時に、現在attemptだけが `failed/generation_unavailable` へcommitされる。
4. running runは `started_at` から180秒超過で `failed/stale` へ回収される。
5. 受付から180秒を超えたqueued runは、workerへ届いてもfailed/staleとなりproviderを呼ばない。
6. 未配送queued runは受付から5分超過でfailed/staleへ回収される。
7. queued期限切れのquota予約はterminal化と同じtransactionで最大1回返却される。
8. running timeout、running stale、provider開始後の失敗ではquotaを返却しない。
9. stale sweeperは毎分起動する。
10. application timeoutとrunning sweepのterminal eventは、DB commit後にbest-effortで発行される。
11. frontendはrequest受付から180秒で「状態更新中」表示へ移り、pollingを継続する。
12. frontendはDB terminal観測後、既存のcompleted / policy_blocked / failed表示へ収束する。
13. backend unit/integration testとfrontend fake-timer testが本仕様の時間・競合・表示契約を固定する。
14. 関連するlint、format、type check、unit testが通る。

## 決定事項

### 固定時間

次を環境変数ではなく、backendまたはfrontend内部の明示的な定数として固定する。

```text
RESEARCH_APPLICATION_TIMEOUT_SECONDS = 150
RESEARCH_TASKIQ_TIMEOUT_SECONDS = 180
RESEARCH_RUNNING_STALE_AFTER_SECONDS = 180
RESEARCH_QUEUED_START_DEADLINE_SECONDS = 180
RESEARCH_QUEUED_STALE_AFTER_SECONDS = 300
RESEARCH_STALE_SWEEP_CRON = "* * * * *"
RESEARCH_UI_DEADLINE_SECONDS = 180
```

150秒はhandler開始から正常なanswer generationへ与える実行予算である。Taskiq側との差である30秒は、
cancel伝播、DB terminal transaction、log、handler returnのための猶予とする。

180秒のTaskiq timeoutは最終安全弁であり、DB terminal化を担当する通常経路ではない。
外側timeoutが先に発火した場合でも、毎分のsweeperとfrontendのUX deadlineで無期限表示を防ぐ。

これらは現在の「3分を超えて通常の生成中表示を続けない」というproduct policyであり、
運用時に環境変数だけで延長できる値にしない。変更時は本仕様、backend、frontend、testを
同じsliceで更新する。

### 期限の役割

| 期限 | 起点 | 役割 | 期限到達時の動作 |
|---|---|---|---|
| queued start deadline 180秒 | DBのrun `created_at` | 遅延した初回実行を禁止する | queuedのままなら `failed/stale` とquota返却 |
| queued stale 300秒 | DBのrun `created_at` | workerへ届かなかったrunを回収する | sweeperが `failed/stale` とquota返却 |
| application timeout 150秒 | Taskiq handler開始 | 正常経路で生成を止め、DBをterminal化する | `failed/generation_unavailable` commitを試行 |
| Taskiq timeout 180秒 | Taskiq callback実行開始 | handler全体が停止しない場合の最終安全弁 | callback cancel、Taskiq result確定、ACKされ得る |
| UI deadline 180秒 | user messageのpersisted `createdAt` | ユーザーが通常の生成中表示を待つ上限 | localな「状態更新中」表示へ移行しpolling継続 |

running stale判定は、DBの `started_at + 180秒` を期限とする。sweeperが毎分起動するため、
DBのbackstop terminal commitは通常、started_atから3〜4分の範囲になる。

この差を隠して「DBは必ず3分でfailedになる」とは定義しない。保証するのは次の4点である。

- 正常なtimeout経路では、answer generationを150秒で止めてterminal commitを試みる。
- DB障害などでterminal化できなくても、画面は180秒で通常の生成中表示をやめる。
- queued runは受付から180秒を超えた後に初回実行を開始しない。
- 未配送queued runのDB terminal commitは、毎分sweepにより通常5〜6分で行われる。

## 状態遷移

### queued初回取得

```text
Taskiq callback開始
  -> DB transaction内でrunを確認
       -> status=queued AND created_at < now - 180秒
            -> failed/staleへ条件付き遷移
            -> 同じtransactionで元usage_dateの日次quotaを1件返却
            -> commit
            -> AnsweringRunner / providerを呼ばずreturn
            -> Taskiq XACK
       -> status=queued AND start deadline内
            -> runningへ条件付き遷移
            -> started_at=now、attempt_epoch=N
            -> quotaは返却しない
            -> commit後にanswer generationへ進む
       -> status=running
            -> 既存の再取得・attempt fencing契約に従う
       -> terminalまたは存在しない
            -> idempotent skip
```

queued expiry判定、`queued -> failed/stale`、quota返却は同じDB transactionで行う。
statusを事前に読んだだけでquotaを返さず、条件付きterminal UPDATEの勝者だけが返却する。

start deadlineの比較は `created_at < now - 180秒` とする。境界ちょうどは期限内とし、
その直後の比較から期限超過になる。application processのwall clock差を混在させないため、判定に使う
`now` は同じDB transactionで確定した時刻とする。

### 正常なtimeout経路

```text
Taskiq callback開始
  -> application deadline = monotonic now + 150秒
  -> start deadline内のrun取得transaction commit
       status=running
       started_at=now
       attempt_epoch=N
  -> application deadlineまでanswer generation
       -> 時間内成功
            -> completed commit
            -> terminal(completed)をbest-effort publish
            -> handler return
            -> Taskiq XACK
       -> 150秒到達
            -> generationをcancel
            -> application timeoutの外側で短いDB transactionを開始
            -> running + attempt_epoch=N の場合だけfailed/generation_unavailable commit
            -> commit成功後にterminal(failed)をbest-effort publish
            -> handler return
            -> Taskiq XACK
```

application timeoutは、timeoutを起こしたcancel scopeの内側でDB cleanupを実行しない。
cancelを `TimeoutError` へ変換した後、timeout scopeの外側でterminal transactionを開始する。
これによりcleanup自身が同じ150秒timeoutによって即時cancelされることを避ける。

### outer timeoutによる異常経路

```text
status=running / attempt_epoch=N
  -> handlerが150秒timeout後のcleanupを完了できない
  -> 180秒Taskiq timeoutがcallbackをcancel
  -> Taskiqはerror resultを確定し、when_executedとしてXACKし得る
  -> DBはrunningのまま残り得る
  -> UIはcreatedAt + 180秒で「状態更新中」へ移行
  -> 毎分sweeperがstarted_at + 180秒超過を検出
  -> failed/stale commit
  -> commit後にterminal(failed, stale)をbest-effort publish
  -> SSEまたはpollingでfrontendがterminalを観測
  -> 既存のfailed表示へ収束
```

この経路では、ACK前に必ずDBを書けることをTaskiqへ期待しない。ACKとDB更新は分散transactionではなく、
ACK済み・DB activeの不整合をDB sweeperで収束させる。

## Backend design

### queued start deadlineの所有者

`AgentRunRepository.acquire_for_execution()` を、単なる `PreparedAgentRun | None` ではなく、
取得・queued期限切れ・idempotent skipを区別できる内部command resultへ変更する。queued期限切れ結果は、
quota返却結果を含み、task側がcommit後observabilityを正しく発行できるようにする。

queuedの初回取得だけは、同じtransaction内で次の順に処理する。

1. DB時刻と `created_at` からstart deadline超過を判定する。
2. 超過していれば `status='queued'` を条件に `failed/stale` へ更新する。
3. UPDATE成功かつ `quota_usage_date IS NOT NULL` なら、元利用日のcounterを1件減算する。
4. terminal transitionとquota減算を同じtransactionでcommitする。
5. AnsweringRunner、live attempt、providerを開始せずtaskを終了する。

quota counter欠損または `used_count=0` で減算できない場合はunderflowさせない。runの期限切れを優先して
`failed/stale` はcommitし、返却結果を `inconsistent` として観測する。quota queryの例外でtransaction自体が
rollbackした場合はrunもqueuedのままとし、5分sweeperへ回収を委ねる。

`status='running'` の再配送はqueued start deadlineの対象にしない。既存のattempt再取得と
running stale回収で処理し、過去にqueuedだったことを理由にquotaを返却しない。

### Application timeoutの所有者

`run_agent_answer()` はhandler開始時にmonotonic clockで150秒後のapplication deadlineを確定する。
run取得に要した時間もこの150秒へ含め、取得commit後はdeadlineまでの残り時間だけを
Python標準のasync timeoutでanswer generationへ与える。新規dependencyは追加しない。

run取得がapplication deadline後に完了した場合はanswer generationを開始せず、取得できたcurrent attemptを
同じtimeout経路でterminal化する。run取得transaction自体がcancelまたはrollbackされて取得できなかった場合は、
存在しないattemptをterminal化しない。

timeout対象には少なくとも次を含める。

- live attempt初期化とreset。
- history読込み。
- `build_answering_runner()` と `answering_runner.run()`。
- answer resultの構築。

timeout後の次の処理はcancel scopeの外側で行う。

- epoch-fencedなfailed transition。
- DB transaction commit / rollback。
- terminal eventのbest-effort publish。
- timeout outcomeのlog / metric。

application timeoutの判定は、所有するtimeout contextが実際にexpiredした事実で行う。
下位providerやSDKが独自に送出した同名の `TimeoutError` だけを見て、application deadline到達と
誤分類しない。

成功結果を得た場合も、DBのcompleted commitがTaskiq 180秒の内側で終わらなければならない。
Taskiq outer timeoutは、停止不能なcleanupやDB I/Oに対する最終安全弁として残す。

### Application timeoutのterminal化

application timeoutを観測したattemptは、既存の `AgentRunRepository.mark_failed()` を利用し、
次の条件付きUPDATEを行う。

```text
WHERE id = :run_id
  AND status IN ('queued', 'running')
  AND attempt_epoch = :expected_attempt_epoch

SET status = 'failed'
    error_code = 'generation_unavailable'
    completed_at = :now
```

実際には取得済みattemptなので通常statusは `running` である。既存repository contractを再利用する場合も、
`attempt_epoch` fencingを外してはならない。

結果ごとの動作は次のとおりとする。

| 結果 | 動作 |
|---|---|
| UPDATE成功・commit成功 | timeout terminalizedを記録し、commit後にterminal eventを発行してreturn |
| UPDATE 0件 | cancel、別attempt、既存terminalとの競合としてlost raceを記録し、上書き・event発行をしない |
| DB transaction失敗 | rollbackし、低cardinalityの失敗を記録して安全なtask境界例外でerror終了する |
| terminal publish失敗 | warningを記録してreturn。DBのfailedは維持する |

DB transaction失敗時に「失敗通知だけ」を先に送ってはならない。Redisのterminal eventを受けた画面が
failedを確定表示した後、DB pollingでrunningへ戻る矛盾を避けるためである。

Taskiq receiverは未捕捉例外の文字列・tracebackをlogとtask resultへ保存し得るため、queued acquire / expiry、
application timeout terminalization、stale sweepのDB境界では、元のSQLAlchemy / driver例外をそのまま
Taskiqへ渡さない。transactionをrollbackした後、固定messageだけを持つ専用例外へ変換し、元例外を
cause、context、属性へ保持せずerror終了する。変換対象は `Exception` に限り、Taskiq outer timeoutや
cancellationに使われる `BaseException` は捕捉しない。

### Taskiq outer timeout

`run_agent_answer` decoratorの `timeout` を300秒から180秒へ変更する。

- `max_retries=0` と `retry_on_error=False` は維持する。
- `--ack-type when_executed` は維持する。
- outer timeoutをretry機構として扱わない。
- timeout例外を成功扱いに変換することだけを目的に、広い `BaseException` catchを追加しない。
- process強制終了、Redis pending再配送、worker再起動は本sliceで再設計しない。

## Stale sweeper design

### queuedとrunningの期限を分離する

現在の `coalesce(started_at, created_at) < now - 20分` という共通条件を廃止し、statusごとに
独立したcutoffを使用する。

```text
queued:
  status = 'queued'
  AND created_at < now - 300秒

running:
  status = 'running'
  AND coalesce(started_at, created_at) < now - 180秒
```

queued start deadlineとsweeper deadlineは責務が異なる。180秒を超えたqueued messageがworkerへ届けば、
acquire commandが直ちに `failed/stale` としてprovider開始を禁止する。message自体が届かない場合は、
sweeperが300秒超過を回収する。毎分起動のため、未配送runの実際のDB回収は通常5〜6分後になる。

runningで `started_at IS NULL` は通常の取得経路では生じない不正rowである。この場合だけ
`created_at` を180秒cutoffのfallbackに使って回収し、同時に構造的不整合をlog/metricで観測する。
不正rowを無期限にactiveのまま残さない。

productionのqueued/running cutoffは、1回のsweep statementで確定したDB時刻から計算する。
candidate抽出とterminal UPDATEで異なるapplication clockを使わない。testでは固定DB時刻を渡せる内部seamで
境界を検証するが、production repositoryのpublic引数として任意の期限値を公開しない。

### 実行間隔

`CRON_AGENT_RUN_SWEEP` を毎分 `* * * * *` に変更し、scheduleのSSoTと時刻表を同時に更新する。

running timeoutは時刻境界とscheduler起動タイミングにより、started_atから3分ちょうどではなく
3〜4分でDB terminal化される。1分より細かいpolling loop、新しいscheduler、常駐watchdogは追加しない。

### 条件付きterminal化と返却契約

sweeperはcandidateをlockし、active statusのrowだけを `failed/stale` へ更新する既存の原子性を維持する。
terminal eventをcommit後に発行できるよう、内部のsweep resultは少なくとも次を区別できる形にする。

- terminal化したqueued件数。
- queued期限切れのquota返却結果別件数。
- terminal化したrunning runの `run_id` と `attempt_epoch`。
- 日次quota observability用のrunning予約維持件数。

queued candidateの `failed/stale` 遷移とquota返却は同じtransactionに参加させる。複数queued runを
回収するときは、terminal化に成功したquota対象runを `(user_id, usage_date)` ごとに集約し、counterを
set-basedに減算する。run件数に比例したquota queryのN+1 loopを作らない。
`user_id` はcandidate取得queryで `agent_threads` から同時に解決し、runごとにthreadを再取得しない。

counter row欠損または減算対象件数より `used_count` が小さいgroupはunderflowさせず、runのterminal化を
優先してcommitし、返却不能件数を `inconsistent` として観測する。transaction自体がrollbackした場合は、
run terminal化とquota減算のどちらも確定結果としてcallerへ返さない。

running terminal eventには、sweepで取得した正の `attempt_epoch` を使用する。transaction commit後に、
各runのRedis Streamへ `terminal(status='failed', errorCode='stale')` をbest-effortで発行する。

1件のterminal publish失敗によって、他runのpublish、sweeper task、commit済みDB状態を失敗させない。
publish失敗は既存のterminal publisherと同じ低cardinality warningで観測する。

queued runはattemptが開始されておらず `attempt_epoch=0` であるため、本sliceではStream terminal eventを
新設しない。既存のDB polling / refreshでterminal状態へ収束させる。

### Sweepとcurrent attemptの競合

application timeout、cancel、normal completion、sweepは同じrunへ同時に到達し得る。

- 先にterminal commitした経路だけが勝つ。
- 後続UPDATEは0件となり、terminal状態を上書きしない。
- sweeperがrunning attemptをterminal化した後、旧workerがcompletedをcommitしてはならない。
- 旧workerからのlive eventは既存のattempt epoch fencingで表示対象から外す。
- terminal eventは、自身のDB transitionが成功・commitした経路だけが発行する。

queuedについては、cancel、期限内acquire、期限超過acquire、5分sweepを同じDB rowの条件付き遷移で
競合させる。

| 勝者 | run | quota | provider |
|---|---|---|---|
| queued cancel | `failed/cancelled` | 1件返却 | 呼ばない |
| 期限内acquire | `running` | 返却なし | 開始可能 |
| 期限超過acquire | `failed/stale` | 1件返却 | 呼ばない |
| 5分queued sweep | `failed/stale` | 1件返却 | 呼ばない |

quota返却は、queuedからterminalへの条件付きUPDATEに成功したcancel / expiry経路だけが所有する。
後からterminal statusを再読して返却せず、同じrunの返却は最大1回とする。

## Frontend design

### UX deadlineの起点

frontendはactive runに対応する `ResearchUserMessage.createdAt` を
`ResearchActiveRunBoundary` / live controllerへ渡す。deadlineは次で求める。

```text
deadline = persisted userMessage.createdAt + 180秒
remaining = max(0, deadline - current browser time)
```

初期remainingを算出した後のtimerはmonotonicな経過時間で管理する。page refresh、SSEからpolling-onlyへの
切替、React componentの再renderによってdeadlineを180秒先へ延長しない。

既存の `createdAt` を利用するため、Pydantic schema、generated TypeScript type、API response shapeは
変更しない。端末時計が著しくずれている場合、表示移行が早まるまたは遅れる可能性は残るが、
backendの150秒/180秒制御には影響しない。

### 180秒後のlocal表示

180秒時点でDB statusが `queued` または `running` なら、frontend内部だけの
`recovery-pending` 表示状態へ移る。これはAPI statusやRedis terminal eventではなく、
「DBがまだterminalでないが、通常の待機時間を超えた」ことを表すpresentation stateである。

表示文言は次で固定する。

> 回答に通常より時間がかかっています。現在の実行状態を確認しています。

この表示では次を守る。

- 「失敗しました」とterminal確定を装わない。
- DBがactiveの間は、実行できない再試行を案内しない。
- 通常の「回答を生成中」「回答を準備中」という表示を続けない。
- SSEが生きていてもDB pollingを継続する。
- requestを自動再送しない。
- partial draftを最終回答として確定しない。
- 既存の停止操作は利用可能なままにする。
- 新しい質問入力は、DB上のactive runがterminalになるまで有効化しない。

pollingまたは有効なterminal eventでDB terminal相当を観測したら、timerと
`recovery-pending` を破棄し、既存の表示へ移る。

| DB terminal | 表示 |
|---|---|
| `completed` | 既存の回答確定・refreshフロー |
| `policy_blocked` | 既存のpolicy blocked表示 |
| `failed/generation_unavailable` | 既存の「回答を生成できませんでした」 |
| `failed/stale` | 既存の「時間切れになりました」 |
| `failed/cancelled` | 既存の「キャンセルしました」 |

### Frontend lifecycle

- active runごとにdeadline timerは1個だけ作る。
- run IDまたはpersisted `createdAt` が変わったら旧timerを破棄する。
- terminal観測、unsubscribe、unmountでtimerを破棄する。
- hidden tabでも絶対deadlineは延長しない。復帰時に期限超過なら即座に表示を切り替える。
- terminal確定後に期限timerが発火しても表示をactiveへ戻さない。
- `recovery-pending` はlive controllerの内部snapshotまたは同責務のpresentation contractで表し、
  server status文字列へ混ぜない。

## Terminal event contract

terminal eventは「失敗したことをDBへ保存した後、その事実を画面へ早く伝える通知」である。
DB terminal commitの代わりではない。

```text
DB terminal UPDATE
  -> transaction commit成功
       -> terminal event publishを試行
            -> 成功: SSEで早く通知
            -> 失敗: DB pollingで収束
  -> transaction rollback / commit失敗
       -> terminal eventを発行しない
       -> running sweeperが後で再評価
```

application timeoutのterminal eventはcurrent `attempt_epoch` を使う。running sweeperのeventは、
sweepでterminal化したrowから取得した `attempt_epoch` を使う。frontendは既存のepoch fencingを維持し、
古いattemptまたはreplayされたterminalを受理しない。

## Failure matrix

| 状況 | DB | Redis / Taskiq | frontend |
|---|---|---|---|
| 150秒以内に成功 | `completed` commit | terminal(completed)、return後ACK | 既存の回答確定表示 |
| queued messageが受付180秒超過後に配送 | `failed/stale` とquota返却をcommit | providerを呼ばずACK | pollingで時間切れ表示へ収束 |
| queued messageが未配送で300秒超過 | sweeperが `failed/stale` とquota返却をcommit | queuedはattempt eventなし | pollingで時間切れ表示へ収束 |
| 150秒application timeout、DB成功 | `failed/generation_unavailable` commit | terminal(failed)をbest-effort publish、ACK | 既存の生成失敗表示 |
| timeout terminal UPDATEが0件 | 先行terminalまたは新attemptを維持 | この経路からeventなし | 正本の状態へ収束 |
| timeout terminal commit失敗 | `running` のまま残り得る | handlerがerror終了し、直後にACKされ得る | 180秒で状態更新中、polling継続 |
| 180秒outer timeout | `running` のまま残り得る | error result・ACKされ得る | 180秒で状態更新中、polling継続 |
| 毎分sweepがrunningを回収 | `failed/stale` commit | commit後terminal(failed)をbest-effort publish | 時間切れ表示へ収束 |
| terminal publish失敗 | terminal状態を維持 | event欠落 | pollingで収束 |
| frontendのSSE障害 | 変更なし | polling-onlyへ劣化 | deadlineとpollingを維持 |
| frontendのpolling一時障害 | 変更なし | exponential backoff継続 | deadline後は状態更新中を維持 |
| userが停止 | `failed/cancelled` commit | 既存terminal/cancel経路 | キャンセル表示へ収束 |

## Quota contract

日次quotaの予約・返却ポリシーの正本は
`backend/specs/agent-user-daily-request-quota-slice.md` とする。本節は、timeoutとqueued expiryが
quotaへ接続する部分だけを要約し、独立した返却ポリシーを定義しない。

本sliceは日次quotaの上限、予約時点、JST利用日を変更しない。返却理由に
「provider開始前のqueued期限切れ」を追加する。

- queued cancelとqueued期限切れは返却対象である。
- queued期限切れは、3分超過後のworker取得時と5分sweep時の両方を含む。
- quota返却は `queued -> failed/stale` と同じtransactionで行い、元の `quota_usage_date` のcounterだけを減算する。
- legacy runなど `quota_usage_date IS NULL` は返却対象外とする。
- counter欠損・underflow条件ではrun terminal化を優先し、quotaは減算せず `inconsistent` を観測する。
- runningへ移行したrunのapplication timeout、Taskiq timeout、stale sweepは解放対象にしない。
- timeout cleanupの失敗やterminal event欠落を理由に、frontendまたはRedisからquotaを補正しない。

enqueue failure、provider failure、generation failure、policy block、completed、running cancelの既存policyは
本sliceで変更しない。queued期限切れはproviderを開始していないことをDB statusで確定できるため、
running以降のtimeoutとは区別する。

## Observability

queued expiry専用の共通metricは追加しない。回収経路ごとのcommit後logと、既存のquota返却metricを
組み合わせて観測する。

| event | 発行点 | 属性 |
|---|---|---|
| `application_timeout_terminalized` | failed commit成功後 | error code、attempt outcome |
| `application_timeout_lost_race` | conditional UPDATE 0件 | attempt outcome |
| `application_timeout_terminalize_failed` | DB rollback後 | exception type |
| `agent_run_queued_start_deadline_expired` | 3分超過queued 1件のterminal commit後 | `quota_release_result` |
| `agent_runs_queued_stale_swept` | 5分sweepのbatch commit後 | `run_count`、quota結果別件数 |
| `running_timeout_swept` | sweep commit成功後 | count |
| `running_without_started_at` | 不正running row検出時 | count |
| `terminal_publish_failed` | Redis publish失敗時 | terminal status |

3分経路の `quota_release_result` は `released / not_eligible / inconsistent` のいずれかとする。
5分sweepはrunごとのlogを作らず、1回のbatch logへ次を含める。

```text
run_count
quota_released_count
quota_not_eligible_count
quota_inconsistent_count
```

quota返却結果の総数には、既存の次のmetricを再利用する。

```text
agent_user_daily_quota_releases_total{
  result="released|not_eligible|inconsistent"
}
```

3分経路はcommit後に1件、5分sweepはcommit後にresult別件数を加算する。このmetricはqueued expiry専用ではなく、
既存のqueued cancelを含むquota返却結果全体を表す。新しい `source` label、queued expiry共通metric、
常設dashboardは追加しない。

既存metric helperは正の `count` を受け取れる内部契約へ拡張し、queued cancelと3分経路は既定値1、
5分sweepはresult別の集計件数を渡す。件数分のmetric emit loopは作らない。

metric labelへ `run_id`、`thread_id`、`user_id`、provider名、質問、例外messageを入れない。
run IDが既存の構造化log運用で許可されている場合だけlogへ残し、metricには使用しない。

運用確認では次を見る。

- application timeout発生数。
- application timeoutからDB terminal commitまでの時間。
- 3分・5分回収logによるqueued期限切れの発生経路。
- running stale sweep件数。
- `running` ageの最大値。
- terminal publish失敗数。

正常な `released` と `not_eligible` は常設可視化・alert対象にしない。
既存metricの `inconsistent > 0` だけをalert対象とし、発生時は同時刻の3分・5分回収logで経路を確認する。

## Test plan

### Backend task tests

1. answer generationが150秒を超えると、current attemptを
   `failed/generation_unavailable` へ1回だけcommitする。
2. timeout cleanupはapplication timeout scopeの外側で実行される。
3. timeout transition commit成功後にだけterminal eventを発行する。
4. terminal publishが失敗してもtaskはDB failedを維持してreturnする。
5. timeout transitionのUPDATEが0件ならeventを発行せず、既存terminalを上書きしない。
6. DB commit失敗時はterminal eventを発行しない。
7. 別attemptへ進んだrunを古いattemptのtimeoutがfailedにしない。
8. 正常完了、policy block、既存provider error、cancelの既存経路を回帰させない。
9. Taskiq decoratorのtimeoutが180秒、retry設定が既存値のままである。
10. 下位処理が独自に送出した `TimeoutError` をapplication deadline到達として誤計測しない。

時間testは実時間で150秒待たず、fake clockまたはtimeout境界を注入可能な内部契約で検証する。
テストのためだけにproductionのdomain policyを環境変数化しない。

### Queued expiry / acquire tests

1. `created_at < now - 180秒` のqueuedは `failed/stale` となり、attemptを取得しない。
2. 期限切れqueuedではAnsweringRunner、live attempt、providerを一度も呼ばない。
3. 180秒境界ちょうどのqueuedは期限内として取得でき、その直後は期限切れとなる。
4. quota対象queuedの期限切れは、run terminal化と元利用日のcounter減算を同じtransactionでcommitする。
5. legacy queuedの期限切れはrunだけterminal化し、quota resultをnot eligibleとする。
6. counter欠損・0ではunderflowせず、runをterminal化してinconsistentを観測する。
7. quota query例外ではrun terminal化もrollbackし、確定済み返却として観測しない。
8. cancelが勝てばcancelだけが返却し、期限切れ取得はidempotent skipする。
9. 期限内acquireが勝てばrunningとなり、期限切れ経路はquotaを返却しない。
10. `status=running` の再配送へqueued start deadlineを適用せず、quotaを返却しない。
11. 3分経路のlogとquota metricはcommit後に1回だけ発行し、rollback時は発行しない。

### Repository / sweeper tests

1. `running` かつ `started_at < now - 180秒` だけを `failed/stale` にする。
2. `started_at` が180秒境界ちょうどのrunは、比較演算子の契約どおり次回対象になる。
3. 180秒未満のrunningを変更しない。
4. queuedは300秒未満ではsweeperが変更せず、300秒超過でfailed/staleにする。
5. completed、policy_blocked、failedを変更しない。
6. sweep結果がterminal化したrunningのrun IDと正のattempt epochを返す。
7. transaction rollback時にterminal対象をcallerへ確定結果として返さない。
8. concurrent completionまたはcancelとの競合でterminal状態を上書きしない。
9. quota対象queuedのterminal化とcounter減算を同じtransactionで確定する。
10. `running` かつ `started_at IS NULL` は `created_at` をfallbackに回収し、異常として観測する。
11. 同一user・usage dateの複数queued返却を集約し、set-basedにcounterを減算する。
12. batchのcounter欠損・underflow groupをinconsistentとして観測し、負数にしない。
13. running staleのquota予約は維持する。

### Sweeper task / schedule tests

1. cron定数が `* * * * *` である。
2. scheduleの時刻表が実装と一致する。
3. DB commit後にrunning stale runごとのterminal eventを発行する。
4. 1件のpublish失敗後も他runのpublishを試行する。
5. publish失敗でsweeper task全体をretryまたはrollbackしない。
6. 5分sweepはcommit後にbatch logを1件だけ発行し、run総数とquota結果別件数を含める。
7. 既存quota metricへresult別件数を1回ずつ加算し、run件数分のemit loopを作らない。

### Frontend tests

fake timerを用いて次を固定する。

1. `createdAt + 180秒` より前は既存のactive表示を維持する。
2. 180秒到達時にactiveなら、「回答に通常より時間がかかっています。現在の実行状態を確認しています。」
   という `recovery-pending` 表示へ移る。
3. deadlineはrerender、SSE再接続、polling-only移行で延長されない。
4. page表示時点ですでにdeadline超過なら、即座に `recovery-pending` を表示する。
5. hidden tabから復帰した時点でdeadline超過なら、即座に表示を切り替える。
6. `recovery-pending` 中もpollingを継続する。
7. terminal観測後にtimerを破棄し、既存のterminal表示へ移る。
8. terminal後に古いtimer callbackが走ってもactive表示へ戻さない。
9. timeout後に自動再送しない。
10. partial draftを最終回答として確定しない。
11. 停止操作を維持する。
12. timer cleanup後にstate updateしない。
13. `recovery-pending` は失敗確定または再試行を案内しない。

### Integration tests

1. handler内application timeoutからDB failed、terminal event、frontend failed表示まで収束する。
2. 3分超過queuedの遅延配送がproviderを呼ばずfailed/staleとquota返却へ収束する。
3. 未配送queuedを5分sweeperがfailed/staleとquota返却へ収束させる。
4. terminal commit失敗を模擬し、Taskiq ACK後に残ったDB runningをsweeperがfailed/staleへ収束させる。
5. handler cleanupのstallでouter timeoutを発火させ、ACK後に残ったDB runningをsweeperが回収する。
6. Redis terminal publish失敗時もDB pollingだけでfrontendがterminal表示へ収束する。
7. sweepとlate completionを競合させ、DB terminalが一度だけ確定する。

## Implementation scope

実装時に変更対象となる責務は次である。ファイル名は現行構成を示し、責務が一致する範囲でのみ変更する。

- `backend/app/queue/tasks/agent_run.py`
  - 3分超過queuedの遅延実行禁止。
  - 150秒application timeout。
  - 180秒Taskiq timeout。
  - timeout outcomeとcommit後terminal publish。
  - sweep後のrunning terminal publish。
- `backend/app/agent/runs/repository.py`
  - queued start deadlineの条件付きterminal化とquota返却。
  - queued 300秒 / running 180秒のcutoff分離。
  - terminal化したrunning run ID / attempt epochの返却。
- `backend/app/agent/runs/contracts.py`
  - acquire / sweep内部結果契約。
- `backend/app/agent/runs/daily_quota/*`
  - queued expiry返却とcommit後observability。
- `backend/app/queue/schedule.py`
  - 毎分cronと時刻表。
- backendの関連test。
- `frontend/src/features/research/live/controller.ts`
  - persisted request時刻からのUX deadlineとlocal recovery state。
- `frontend/src/features/research/hooks/useResearchRunLiveState.ts`
  - deadline入力の受渡しが必要な場合のみ変更。
- `frontend/src/features/research/components/ResearchThreadView.tsx`
  - user messageの `createdAt` をactive run境界へ渡す。
- `frontend/src/features/research/components/ResearchThreadLiveBoundary.tsx`
  - recovery-pending文言とpresentation。
- frontendの関連test。

次は変更しない。

- Pydantic schemaとgenerated TypeScript API types。
- Alembic migrationとSQLAlchemy model。
- brokerのStream名、consumer group、`xread_count`、`idle_timeout`。
- frontend submit APIとquota response contract。

## Implementation order

1. backendのtimeout/sweeper testで現在の失敗を再現する。
2. frontendのfake-timer testで180秒後の表示契約を固定する。
3. queued start deadlineと原子的quota返却を実装する。
4. application timeoutとepoch-fenced terminal化を実装する。
5. Taskiq outer timeoutを180秒へ変更する。
6. queued/running cutoffを分け、sweeperを毎分化する。
7. queued sweepのset-based quota返却とrunningのcommit後terminal eventを実装する。
8. frontendのpersisted deadlineとrecovery-pending表示を実装する。
9. backend/frontendのlint、format、type、unit/integration testを実行する。
10. 実装差分と本仕様のDoneを照合し、直接関係しないqueue再設計は別taskへ残す。

## Residual risk

- Taskiq outer timeoutとDB updateは分散transactionではないため、ACK済み・DB runningの瞬間的不整合は残る。
  毎分sweeperとfrontend deadlineで有限時間内に収束させる。
- sweeper自体またはschedulerが停止している間、DB runningは自動terminal化されない。frontendは
  180秒後に状態更新中へ移るが、DB terminalには運用復旧が必要である。
- requestがqueuedのまま3分を超えた場合、frontendは状態更新中へ移り、以後の初回provider実行は禁止する。
  messageが配送されなければDB rowは毎分sweeperの次回判定まで残り、通常5〜6分でterminalになる。
- deadline直前にrunningへ遷移したrunは、handler開始から最大150秒のgeneration budgetを持つため、
  request受付から3分を超えてbackend処理が続く可能性がある。3分はUI待機とqueued初回開始の期限であり、
  end-to-end DB terminal deadlineではない。
- user messageのserver timestampとbrowser clockの大きなずれにより、UX deadline表示が早まるまたは遅れる
  可能性がある。backend timeoutとsweeper判定はserver timeで行う。
- providerや下位SDKがcancellationを速やかに処理しない場合、Taskiq outer timeoutまでhandlerが残る可能性がある。
- terminal eventはbest-effortなので欠落し得る。DB pollingが最終的な収束経路である。

## Research note

本仕様のTaskiq ACK挙動は、workspaceにinstall済みのTaskiq 0.12.4のreceiver sourceと、
現在の `--ack-type when_executed` 設定を組み合わせて確認した。将来Taskiqをupgradeする場合は、
timeout exceptionの捕捉位置、error result生成、ACK条件を新versionのsourceとtestで再確認する。

Pythonのasync timeoutはcancelを利用するため、cancel scopeの外でterminal cleanupを行う設計とする。
Redisのterminal eventは通知速度のためのbest-effort経路であり、DB commitを状態の正本とする既存契約を
変更しない。
