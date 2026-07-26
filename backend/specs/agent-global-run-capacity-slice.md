# Agent global run capacity admission slice 仕様

Status: Deferred — 2026-07-22

Date: 2026-07-21

> **保留理由**
>
> 本仕様のglobal capacity admissionとRedis transport recoveryは、Research専用Streamの
> 責務・配送・回収設計を整理した後に再評価する。現時点では実装しない。
> Taskiq timeoutによってDBの `running` が残り得る問題だけは切り離し、
> `backend/specs/agent-run-timeout-terminalization-slice.md` を正本として先に解消する。

## 位置付け

Vector の Research は、認証済みユーザーの質問を Postgres の `agent_runs` に
`queued` として保存し、commit 後に Taskiq の `agent` Redis Stream へ enqueue する。
worker は run を取得した時点で `running` へ遷移させ、成功・失敗・policy block・cancel・
stale sweep のいずれかで terminal にする。

本 slice は、全ユーザー横断の Research run 受付容量を Postgres で原子的に制限し、
長時間滞留による待ち時間・worker memory・provider 費用の無制限な増加を防ぐ。
同時に、Taskiq 外側 timeout だけでは `running` が terminal にならない経路と、
taskiq-redis 1.2.3 の stale pending 回収が新規 message に依存する経路を hardening する。

本 slice は次の既存仕様へ追加する。

- `specs/agent/conversation-history-async-runs.md`
- `backend/specs/agent-history-run-execution-slice.md`
- `backend/specs/agent-attempt-epoch-fencing-token-slice.md`
- `backend/specs/agent-user-daily-request-quota-slice.md`

2026-07-21 のユーザー合意:

- 受付容量の正本は Redis の `lag` / `pending` ではなく Postgres の run status とする。
- 全ユーザー横断で `queued < 10` かつ `queued + running < 20` のときだけ受理する。
- 複数 API process 間の判定は PostgreSQL transaction advisory lock で直列化する。
- 物理的な同時実行数は、1 worker process の `--max-async-tasks 10` で別に制御する。
- capacity は Taskiq ACK ではなく、DB の terminal status commit で解放する。
- 上限超過は typed `503 Service Unavailable` と `Retry-After: 60` で返す。
- 上限値は環境変数ではなく正の domain 定数とする。
- Redis の `lag` / `pending` / pending age は受付判断ではなく配送回復と監視に使用する。
- production deploy、複数 worker Machine への scale-out、provider call単位の予算制御は別 gate とする。

## Problem

現状は、異なる thread から全ユーザー横断で作成できる active run 数に上限がない。
同一 thread の active run は partial unique indexで1件に制限され、ユーザー日次quotaも存在するが、
複数ユーザーまたは複数threadからの正当なrequestが同時に集中した場合、次を防げない。

- workerが処理できる量を超える `queued` runの蓄積。
- ユーザー待ち時間の無制限な増加。
- worker復旧後に多数のprovider requestが連続して発生する費用スパイク。
- worker crash、timeout、Redis pending回収遅延によりcapacityが長時間占有されること。

Redis Streamの値を受付正本にすると、制限対象とtransport内部状態が一致しない。

- `XLEN` はACK済みの保持entryを含み、現在の待機数ではない。
- `lag` はconsumer groupへ未配達のentryだけを表し、workerがprefetchした未開始taskを含まない。
- `pending` はprefetch、実行中、worker crash、malformed message、未登録taskを区別しない。
- taskiq-redisは一度の `XREADGROUP` で複数messageをpendingへ移し得る。
- `agent` Streamにはuser Researchだけでなくstale sweeper等のcron taskも入る。
- Redis再起動、group再作成、trim/deleteにより観測値の意味または可用性が変わり得る。
- `XINFO`で確認してから `XADD` するcheck-then-actは、同時requestに対して原子的でない。

一方、DBの `queued` は「受理済みだがResearch callbackが開始されていないrun」、`running` は
「Research callbackが開始され、terminal commit前のrun」を表す。今回制限したいdomain状態と一致し、
run作成transactionへ同じDB内で参加できる。

また、現在の `run_agent_answer` はTaskiq task timeoutを300秒に設定している。Taskiq receiverの
外側timeoutがhandlerをcancelすると、Taskiqはerror resultを作成してACKできる一方、handler内の
`failed` commitへ到達できず、DB runが `running` のまま残る可能性がある。このrunは20分のstale閾値と
10分間隔sweepにより概ね20〜30分capacityを占有する。

taskiq-redis 1.2.3の `RedisStreamBroker.listen()` は、新規messageを `XREADGROUP` で取得できた場合だけ
その後の `XAUTOCLAIM` へ進む。stale pendingだけが存在し新規messageがない場合、自動回収は進まない。
現在は10分ごとのstale sweeper messageが偶発的なwakeになるが、回収時刻を明示的に保証する契約ではない。

## Evidence

- `app/agent/router.py::create_research_response()` は、run作成transactionをcommitした後に
  `run_agent_answer.kiq()` を呼ぶ。
- `app/agent/runs/repository.py::create_user_run()` は、thread所有権、同一thread active run、日次quota、
  user message、queued run作成を1つのadmission commandとして扱う。
- `app/agent/runs/repository.py::acquire_for_execution()` は、Taskiq callback開始後にactive runを
  `running` へ遷移し、`attempt_epoch` を増加させる。
- `app/models/agent_run.py` は `status IN ('queued', 'running')` のpartial unique indexを持つが、
  global active run数の上限は持たない。
- `app/queue/tasks/agent_run.py::run_agent_answer()` は `timeout=300`、`max_retries=0`、
  `retry_on_error=False` である。
- `backend/supervisord/agent.conf` は1 worker process、`--max-async-tasks 10`、
  `--ack-type when_executed` で起動する。
- `app/agent/runs/repository.py::sweep_stale_runs()` は、`queued` / `running` のまま20分経過したrunを
  terminal `failed/stale`へ遷移させる。
- `app/queue/schedule.py::CRON_AGENT_RUN_SWEEP` はstale sweeperを10分間隔で起動する。
- `app/queue/brokers.py::_make_broker()` は `idle_timeout=600_000`、`maxlen=10_000`、
  `unacknowledged_batch_size=100` を指定するが、`xread_count`を指定していない。
- workspaceのtaskiq-redisは1.2.3、Taskiqは0.12.4である。taskiq-redis 1.2.3の
  `xread_count`既定値は100で、`listen()` は新規messageを取得できない場合に
  `XAUTOCLAIM`へ進まない。
- `tests/queue/test_analysis_stream_transport_integration.py` は、taskiq-redis 1.2.3では
  persistent foreign lockが存在してもpipeline上の `XAUTOCLAIM` が進むことを固定している。
- `app/queue/stream_health.py` は `XLEN`、`lag`、`pending`、最古entry ageを区別して読めるが、
  現在の定期観測対象はpipeline 4 stageだけで `agent` を含まない。
- 親仕様は、DBを復元・履歴・最終状態の正本、Redisを消失可能なライブ情報と定義している。

## Goal

1. 全ユーザー横断のqueued runを最大10件に制限する。
2. 全ユーザー横断のqueued + running runを最大20件に制限する。
3. 複数API processから同時requestが到達しても、DB commit後の上限を超えない。
4. capacity拒否時はthread、message、run、日次quota予約、enqueue、provider callを残さない。
5. 物理的な同時実行は、1 worker processの `--max-async-tasks 10` を維持する。
6. capacity解放をDB terminal commitへ一本化し、Redis ACKや観測値に依存させない。
7. 通常の300秒Taskiq timeoutより先に、handler内で確実にfailed terminalへ収束させる。
8. worker crash後のstale pendingを、おおむね10〜11分で再配送対象にする。
9. Redis transportの滞留・回収失敗を、受付可否と独立して観測できるようにする。
10. frontendが日次quota 429、capacity 503、generic 503を安全に区別し、入力を保持する。

## 用語と責務境界

### Queued capacity

全ユーザーの `agent_runs.status = 'queued'` の件数。上限は正のdomain定数10件とする。
Taskiq transportの `lag` だけを意味せず、workerにprefetch済みでも
`acquire_for_execution()`がcommitする前のrunを含む。

### Active capacity

全ユーザーの `agent_runs.status IN ('queued', 'running')` の件数。上限は正のdomain定数20件とする。
1runの再配送または `attempt_epoch`増加は新しいcapacityを予約しない。

### Physical concurrency

同時に実行できるTaskiq callback数。現在は1 worker processの `--max-async-tasks 10` が所有する。
DB active capacityとは別の上限である。

### Admission authority

Postgresのcommitted `agent_runs.status`。Redis、Logfire、frontend、BFF、worker process内queueを
受付判定の正本にしない。

### Capacity release

active runがDB transactionでterminal statusへcommitされた時点。Taskiq `XACK`、Redis entry trim、
SSE terminal publish、frontend polling完了をrelease条件にしない。

### Transport recovery

ACK前にconsumerが失われたpending entryを、`XAUTOCLAIM`で生存workerへ再配送すること。
同じrun_idの再配送であり、新しいDB capacityを作らない。

## Fixed policy / runtime constants

次をbackend内部の固定値とする。

```text
RESEARCH_ADMISSION_LOCK_NAMESPACE = 1_447_379_796  # 0x56454354 = "VECT"
RESEARCH_CAPACITY_LOCK_KEY = 1
RESEARCH_QUEUED_RUN_LIMIT = 10
RESEARCH_ACTIVE_RUN_LIMIT = 20
RESEARCH_CAPACITY_RETRY_AFTER_SECONDS = 60
RESEARCH_APPLICATION_TIMEOUT_SECONDS = 270
RESEARCH_TASKIQ_TIMEOUT_SECONDS = 300
RESEARCH_PENDING_IDLE_TIMEOUT_MILLISECONDS = 600_000
RESEARCH_PENDING_RECOVERY_TICK_SECONDS = 60
```

queued / active上限とRetry-Afterはdomain/API policy、timeout / pending recovery値はruntime contract、
advisory lock値はDB process間の識別子である。いずれも環境変数、request、repository public引数、
user role、planから変更できる値にしない。変更時は本仕様、worker構成、API schema、frontend文言、
testを同じsliceで改訂する。

## 全体フロー

```text
POST /api/v1/research/responses
  ├─ Pydantic validation / authentication / agent configuration check
  └─ DB transaction
       ├─ existing threadの所有権確認
       ├─ 同一thread active run競合確認
       ├─ global transaction advisory lock取得
       ├─ queued / active countを同じsnapshotで集計
       │    ├─ queued >= 10 -> rollback -> typed 503
       │    └─ active >= 20 -> rollback -> typed 503
       ├─ 日次quota予約
       ├─ user message作成
       ├─ AgentRun(status=queued)作成
       └─ commit
            ├─ capacity accepted観測
            ├─ Taskiqへrun_idだけenqueue
            └─ 202 {threadId, runId}

worker
  ├─ DB transaction: queued/running -> running、attempt_epoch + 1
  ├─ handler内270秒application timeout
  ├─ AnsweringRunner.run()
  ├─ 成功 -> completed commit
  ├─ policy block -> policy_blocked commit
  ├─ 通常エラー / application timeout -> failed commit
  └─ return後にTaskiqがXACK

transport recovery
  ├─ agent brokerはxread_count=1
  ├─ 1分間隔のrecovery wake message
  ├─ idle 10分以上のpendingをXAUTOCLAIM
  ├─ active runならattempt_epochを進めて再実行
  └─ terminal runなら冪等skipしてXACK
```

## Invariants

- committed `queued` run数は10件以下である。
- committed `queued + running` run数は20件以下である。
- queued上限10とactive上限20は全ユーザー・全thread横断である。
- user起点のrun作成は `AgentRunRepository.create_user_run()` を迂回しない。
- すべてのuser run admissionは同じtransaction advisory lock keyを使用する。
- advisory lock取得後にactive countを読み、同じtransactionでqueued runをinsertする。
- advisory lockはtransaction終了時にだけ解放し、countとinsertの間で解放しない。
- advisory lock keyにPythonのprocess-randomized `hash()`を使用しない。
- capacity判定、日次quota予約、message、runは同じtransactionでcommitまたはrollbackする。
- capacity判定は日次quota予約より前に行う。
- capacity拒否時は新規thread、message、run、quota予約を残さない。
- capacity拒否時はTaskiq enqueue、AnsweringRunner、providerを起動しない。
- capacity DB判定不能時はfail-closedとし、allowへfallbackしない。
- Redisの `XLEN`、`lag`、`pending` をadmission分岐に使用しない。
- Taskiq再配送、running再取得、`attempt_epoch`増加でactive capacityを追加しない。
- capacityはDB terminal commitでのみ解放する。
- Redis ACK失敗または遅延を理由にDB terminal runをactiveへ戻さない。
- application timeoutはTaskiq timeoutより短くし、failed commitの猶予を残す。
- application timeoutのfailed遷移はattempt epochでfenceし、古いattemptが新しいattemptを終了させない。
- Redis recovery wakeは新しいResearch runまたはDB capacityを作らない。
- agent brokerの `idle_timeout` はTaskiq timeoutより長く維持する。
- malformed message、未登録task、stale pendingを0件扱いせず、transport異常として観測する。
- log、metric、503 bodyにuser_id、question、history、provider raw responseを含めない。

## Non-goals

- user単位active run上限の追加または変更。
- 同一thread active run 1件制約の変更。
- ユーザー日次quota 10件の変更。
- IP単位の不正request検知。
- provider call、token、検索agent fan-out単位の全体予算制御。
- `agent_runs`以外のpipeline queue容量制御。
- Redisを使った分散semaphoreまたはPostgres/Redis二重書込み。
- `XLEN`を現在の待機数として扱うこと。
- malformed message用dead-letter queue。
- 自動的なTaskiq retryの有効化。
- 複数worker Machine間で物理実行数を10件に保つglobal execution lease。
- 上限値を環境変数または管理画面で変更する機能。
- production deploy、Fly scale変更、Neon/Redis/provider設定変更。
- production alert通知経路の作成と到達確認。

## 設計判断

### 1. 正本はPostgresのrun statusとする

admissionが制限するのはtransport entryではなく、受理済みResearch runである。
`queued`はcallback開始前、`running`はcallback開始後からterminal commit前を表すため、
ユーザー待ち時間と費用発生可能性の境界に一致する。

Redisの `lag` / `pending` は配送診断に有用だが、prefetch、sweeper、malformed message、group再作成等の
transport状態を含む。これらを503判断へ混在させない。

### 2. Transaction advisory lockでadmissionを線形化する

単純な `COUNT -> INSERT` は、同時transactionが同じcountを読み上限を超えられる。
全user run admissionで共通のPostgreSQL transaction advisory lockを取得し、lock取得後にcountし、
run insertを含むtransaction commitまで保持する。

概念SQL:

```sql
SELECT pg_advisory_xact_lock(:namespace, :capacity_key);

SELECT
  count(*) FILTER (WHERE status = 'queued') AS queued_count,
  count(*) AS active_count
FROM agent_runs
WHERE status IN ('queued', 'running');
```

`:namespace = 1_447_379_796`、`:capacity_key = 1` をparameter bindingする。文字列連結、
Python `hash()`、request由来値を使用しない。repositoryはcommitせず、routerが所有する
既存transactionへ参加する。

PostgreSQLの現行 `READ COMMITTED` を前提とする。transaction isolationを変更する場合は、
lock待ち後のcountが直前admissionのcommitを観測できることを再検証する。

### 3. 2つの上限を独立して適用する

```text
queued_count >= 10 -> reject
active_count >= 20 -> reject
otherwise          -> accept
```

queued上限だけでは、worker crash等でrunningが増えた状態の総負荷を制限できない。
active上限だけでは、runningが少ない時に20件近い待機runを受理できる。両方を満たす必要がある。

count queryはactive statusだけへ絞る。既存partial indexを利用可能なpredicateを維持し、全履歴runを
applicationへ読み出さない。実装時に代表データで実行計画を確認するが、plannerが選ぶplan名を
自動testへ固定せず、必要性のないschema/index追加は行わない。

### 4. 既存エラー優先順位を維持してcapacityを追加する

existing threadでは所有権と同一thread active競合をcapacityより先に確定し、既存404/409を維持する。
capacityは日次quota予約より前に判定する。

| condition | HTTP | capacity / quota effect |
|---|---:|---|
| unauthenticated | 401 | 変更なし |
| request validation失敗 | 422 | 変更なし |
| agent configuration unavailable | generic 503 | 変更なし |
| thread not found / not owned | 404 | 変更なし |
| same-thread active run conflict | 409 | 変更なし |
| global capacity reached | typed 503 | 変更なし |
| daily quota reached | typed 429 | 変更なし |
| accepted | 202 | queued 1件、日次quota 1件予約 |

capacity query、advisory lock、transactionが予期せず失敗した場合はgeneric 5xxへ収束し、
`research_capacity_exceeded`を偽装せず、allowへfallbackしない。

### 5. Capacity releaseはDB terminal commitとする

| transition | capacity release |
|---|---|
| completed commit | 1件 |
| policy_blocked commit | 1件 |
| failed commit | 1件 |
| enqueue_failed commit | 1件 |
| queued/running cancel commit | 1件 |
| stale sweep commit | 1件 |
| Taskiq XACKだけ | 0件 |
| Redis trim/deleteだけ | 0件 |
| SSE terminal publishだけ | 0件 |

terminal transitionと同時に別counterを減算しない。capacityはrun statusから導出するためcounter driftがなく、
worker crash後の再配送も同じrowを再利用する。

### 6. Handler内application timeoutを先に置く

`run_agent_answer` のTaskiq timeout 300秒は最後の安全弁として維持する。その内側で270秒の
application timeoutを設定し、timeoutを捕捉してepoch-fencedな `failed/generation_unavailable` を
短いDB transactionでcommitしてからreturnする。

```text
270秒: application timeout
  └─ child処理をcancel
  └─ attempt_epoch一致を条件にfailed commit
  └─ terminal eventをbest-effort publish
  └─ handler return

300秒: Taskiq outer timeout
  └─ application cleanupも停止した場合の最後の安全弁
```

failed commit自体が失敗または300秒を超えた場合、TaskiqはACKし得るがDB runはactiveに残り得る。
このbackstopはstale sweepとtimeout observabilityが担い、成功したように見せない。

### 7. taskiq-redis recoveryをadmissionから独立してhardeningする

agent brokerだけ `xread_count=1` とし、最大100件が一度にpendingへ移る既定prefetchを抑える。
これは物理並列を1へ下げる変更ではなく、listenerはworker semaphoreの空きに応じて1件ずつ取得し、
`--max-async-tasks 10` は維持する。

taskiq-redis 1.2.3は新規message取得後だけ `XAUTOCLAIM`へ進むため、agent brokerへ1分間隔の
軽量なrecovery wake taskを投入する。wake taskはDB run、quota、provider callを作らず、
`max_retries=0`、`retry_on_error=False`、短いtimeoutで正常returnする。

`idle_timeout=600_000` は維持する。これによりworker crash直後のmessageを生きた処理から奪わず、
最短10分、1分tick込みでおおむね10〜11分後にclaim対象とする。

`unacknowledged_lock_timeout=60` はagent brokerにも明示し、persistent lock keyを残さない補助hardeningとする。
taskiq-redis 1.2.3ではlockの存在自体をXAUTOCLAIM停止条件にしないため、この値を回収保証の根拠にはしない。

taskiq-redisを更新した場合は、idle時にもXAUTOCLAIMするようになったかをintegration testで確認する。
upstreamが自律回収する場合だけrecovery wake taskの削除を別変更として検討する。

### 8. Worker scaleは別の不変条件として扱う

`--max-async-tasks 10` はworker processごとの上限であり、global上限ではない。本sliceでは
`worker-agent`を1 process・1 Machineで運用することを前提とする。

Machine数またはTaskiq `--workers`を増やす場合、DB active上限20の範囲で物理同時実行が10を超え得る。
scale-out前に、1台運用を維持するか、DB/Redisのglobal execution leaseを別gateで設計する。

1 Research run内のTavily/Gemini等の並列provider call数はworker callback数と一致しない。
provider call数または費用を直接10に制限する場合は、別のprovider直前gateで扱う。

## API Contract

対象endpoint:

```text
POST /api/v1/research/responses
```

成功202、thread 404、same-thread 409、日次quota 429の既存契約は変更しない。

global capacity到達時は `503 Service Unavailable` とtyped flat bodyを返す。

```json
{
  "detail": "Research capacity is temporarily unavailable",
  "code": "research_capacity_exceeded",
  "retryAfterSeconds": 60
}
```

Pydantic SSoT:

```python
class ResearchCapacityExceededResponse(_CamelBase):
    detail: Literal["Research capacity is temporarily unavailable"]
    code: Literal["research_capacity_exceeded"]
    retry_after_seconds: Literal[60]
```

response header:

```text
Retry-After: 60
Cache-Control: no-store
```

bodyに `limit`、`queued`、`running`、Redis `lag` / `pending`を含めない。frontend consumerは
満杯の事実と再試行時間だけを必要とし、内部capacityを公開API契約にしない。

503 schemaをrouteの `responses` にmodelとして登録し、OpenAPI generated typeを `unknown` にしない。
flat bodyを維持し、`HTTPException(detail=dict)`による `detail` 二重nestを作らない。

generic 503は既存どおりtyped codeを持たない。frontendはHTTP statusだけでcapacityと判断せず、
`status == 503` と `code == "research_capacity_exceeded"` と有効な非負整数
`retryAfterSeconds`をすべて確認する。

このcodeはrun作成前のadmission errorであり、永続run用 `ResearchRunErrorCode` へ追加しない。

## Frontend / BFF Contract

submit action resultへcapacity variantを追加する。

```typescript
type SubmitResearchQuestionResult =
  | { kind: "accepted"; run: ResearchRunStartResponse }
  | {
      kind: "daily-request-limit-exceeded";
      resetAt: string;
      retryAfterSeconds: number;
    }
  | {
      kind: "capacity-exceeded";
      retryAfterSeconds: number;
    };
```

capacity bodyが有効なら、正の整数 `Retry-After` headerを優先し、欠損または不正時はbodyの
`retryAfterSeconds`を使用する。body自体が不正、code不一致、unknown 503の場合は既存generic errorへ
fallbackする。BFFまたはfrontendでcapacityを事前判定しない。

UI contract:

- 入力した質問を保持する。
- thread redirect、refresh、revalidateを行わない。
- 自動再送しない。
- ユーザーが同じ入力を手動で再送できる。
- `retryAfterSeconds`から待ち時間文言を導出し、「1分」を別定数でhardcodeしない。
- 表示文言は次の意味を維持する。

```text
現在リサーチのリクエストが集中しています。
この質問は受け付けられませんでした。
1分ほど待ってから、もう一度お試しください。
```

「送信されていません」ではなく「受け付けられませんでした」とし、裏で自動実行されないことを
明示する。backendの英語 `detail` をユーザー向け文言として直接表示しない。

## Failure matrix

| condition | DB run | quota | Redis task | HTTP / recovery |
|---|---|---|---|---|
| capacity到達 | 作成なし | 変更なし | 作成なし | typed 503 |
| capacity DB判定失敗 | 作成なし | 変更なし | 作成なし | generic 5xx |
| admission後のtransaction rollback | 作成なし | rollback | 作成なし | 5xx |
| enqueue成功 | queued | 予約維持 | 1件 | 202 |
| enqueue失敗、failed記録成功 | failed | 既存quota仕様どおり維持 | なし | 既存どおり202 |
| 通常実行成功 | completed | 維持 | ACK | capacity解放 |
| 通常実行エラー | failed | 維持 | ACK | capacity解放 |
| application timeout | failed | 維持 | ACK | capacity解放 |
| Taskiq outer timeoutだけ発火 | runningの可能性 | 維持 | ACKの可能性 | stale sweep backstop |
| worker crash before ACK | queued/running | 維持 | pending | 10〜11分目標で再配送 |
| 再配送 | 同じrunをrunning再取得 | 追加予約なし | pending | attempt_epochでfence |
| terminal run再配送 | terminal維持 | 変更なし | ACK | 冪等skip |
| queued/running cancel | terminal | 既存quota仕様に従う | entryは残り得る | capacity解放、後続skip |
| stale sweep | failed/stale | 既存quota仕様どおり維持 | entryは残り得る | capacity解放、後続skip |
| malformed / unknown task | DB runと無関係 | 変更なし | pendingに残り得る | transport alert |

## Observability

### Structured log

| event | level | timing |
|---|---|---|
| `agent_research_capacity_admitted` | info | admission transaction commit後 |
| `agent_research_capacity_rejected` | info | capacity rollback後 |
| `agent_research_application_timed_out` | warn | failed commit結果確定後 |
| `agent_research_capacity_observation_failed` | error | DB count/lockが失敗した場合 |
| `agent_queue_health_observation_failed` | warn | agent Stream snapshot失敗時 |

admitted logにはrun_id、判定時queued/active count、固定limitを含めてよい。rejectedにはrun_idが存在しないため、
判定時queued/active countと固定reason `queued_limit|active_limit`だけを含める。通常logにuser_id、thread_id、
question、history、provider raw responseを含めない。

transaction commit前にadmitted logを出さない。観測書込み失敗をadmission transactionへ参加させない。

### Metric

```text
agent_research_capacity_admissions_total{
  result="accepted|rejected",
  reason="accepted|queued_limit|active_limit"
}

agent_research_application_timeouts_total{
  result="terminalized|terminalize_failed|lost_race"
}

vector.agent.queue.lag
vector.agent.queue.pending
vector.agent.queue.oldest_undelivered_enqueue_age
vector.agent.queue.oldest_pending_enqueue_age
vector.agent.queue.oldest_outstanding_enqueue_age
vector.agent.queue.observation_up
```

metric attributeにuser_id、thread_id、run_id、question、error文字列を含めない。
`XLEN`相当のretained entriesを観測する場合も、queue backlogまたはadmission capacityとして表示しない。

production alert ruleと通知到達確認はdeploy前gateで行う。本sliceは、少なくとも次をalert可能なmetricとして
出せるところまでをapplication Doneとする。

- capacity拒否の継続。
- application timeoutの発生。
- oldest pending ageがidle timeoutを超えること。
- agent queue health observation失敗。

## Tests

### Capacity policy / query

1. queued 9 / running 0は受理でき、commit後queued 10となる。
2. queued 10はactive 20未満でも拒否する。
3. queued 9 / running 10は受理でき、commit後active 20となる。
4. queued 5 / running 15はqueued 10未満でもactive上限で拒否する。
5. terminal statusはqueued/active countへ含めない。
6. 上限はcaller引数または設定値で変更できない。
7. advisory lock keyが固定整数で、transaction終了時に解放される。
8. countまたはlock失敗時にrun作成へ進まない。
9. 多数のterminal runが存在してもcount結果へ混入せず、active statusだけをDB側で集計する。

### Concurrency

1. 独立DB sessionから空状態へ20件以上同時admissionしても、queued commitは10件だけとなる。
2. running 10件をseedし、20件以上同時admissionしても、追加queued commitは10件だけとなる。
3. queued 5 / running 15からの同時admissionは全件拒否する。
4. rollbackしたadmissionがlockまたはcapacityを残さない。
5. terminal transitionとadmissionの競合は、過剰受理せず、古いactiveを数えた場合は保守的拒否へ収束する。
6. 同一thread 409とglobal capacity判定が競合しても、partial unique index違反を500として露出しない。

### API / quota integration

1. capacity拒否はtyped flat 503、`Retry-After: 60`、`Cache-Control: no-store`を返す。
2. capacity拒否時にthread、message、run、daily quota rowを新規作成・更新しない。
3. capacity拒否時にenqueueを呼ばない。
4. thread 404、same-thread 409がcapacityより先に返る。
5. capacity 503がdaily quota 429より先に返り、quotaを予約しない。
6. generic 503にcapacity codeを付けない。
7. response bodyに内部limit、queued、running、lag、pendingを含めない。
8. OpenAPIが503 response modelを生成し、frontend generated typeへ届く。

### Application timeout

1. 270秒超過時にcurrent attemptだけをfailed/generation_unavailableへterminal化する。
2. timeout failed commit後にhandlerが300秒前にreturnできる。
3. 古いattemptのtimeoutは新しいattemptをfailedにしない。
4. terminal transition競合敗北は既存terminalを上書きしない。
5. failed commit失敗時は成功扱いせず、timeout metricを `terminalize_failed` として記録する。
6. outer Taskiq timeoutだけが発火したrunning runをstale sweepが最終的にfailedへ倒す。

### Redis transport integration

1. agent brokerの `xread_count == 1`、idle timeout 600,000ms、lock timeout 60秒を固定する。
2. user run成功・通常失敗・application timeout後にXACKされpendingから外れる。
3. worker crashを模したidle pendingが10分未満ではclaimされない。
4. stale pendingだけではtaskiq-redis 1.2.3 listenerが回収へ進まない現行挙動を固定する。
5. recovery wake messageによりidle pendingがXAUTOCLAIMされ再配送される。
6. persistent foreign lockが存在しても1.2.3のXAUTOCLAIMが進み、lock token自体は勝手に削除しない。
7. terminal DB runの再配送はproviderを呼ばず正常returnしてXACKする。
8. wake taskはrun、message、quota、provider callを作らない。
9. scheduler routingがrecovery wakeをagent Streamへ投入する。

### Frontend

1. status 503、capacity code、有効なretryAfterSecondsをcapacity variantへnarrowする。
2. unknown/generic 503、不正bodyはgeneric errorへfallbackする。
3. 有効な `Retry-After` headerを優先し、欠損・不正時はbody値を使用する。
4. capacity拒否時に入力を保持し、redirect、revalidate、refresh、自動再送を行わない。
5. 「受け付けられませんでした」を含む専用文言を表示する。
6. 表示時間をretryAfterSecondsから導出する。
7. 日次quota 429の既存分岐を維持する。

### Observability

1. acceptedはcommit後だけ、rejectedはrollback後だけ記録する。
2. queued_limitとactive_limitを固定reasonで区別する。
3. timeoutのterminalized / terminalize_failed / lost_raceを区別する。
4. agent queue healthの正常値とRedis/group/lag不明を区別する。
5. log/metricにuser inputと高cardinality属性を含めない。

## Implementation scope

### Backend

- global capacityのdomain constants、typed error、advisory lock + count query。
- `AgentRunRepository.create_user_run()` admissionへのcapacity参加。
- routerのtyped 503 responseとOpenAPI登録。
- handler内application timeoutとepoch-fenced terminal化。
- agent brokerの `xread_count=1`、finite lock timeout。
- 1分間隔recovery wake task、schedule、scheduler routing test。
- agent Redis Stream healthの低cardinality log/metric。
- DB concurrency、API、timeout、transport integration test。

### Frontend

- OpenAPI generated type同期。
- typed capacity errorのruntime narrowing。
- submit action resultのcapacity variant。
- Research composerの入力保持と専用文言。
- backend `Retry-After`の伝播とfallback test。

### Unchanged boundaries

- PydanticをAPI SSoTとする。
- 認証・user ownership・404秘匿境界。
- 同一thread active run partial unique index。
- 日次quotaの予約・queued cancel返却policy。
- AnsweringRunnerのDB/Taskiq非依存性。
- run payloadがrun_idだけを運ぶこと。
- enqueue失敗時にfailed rowを残して202を返す既存契約。
- Redis live eventがnon-authoritativeであること。

## Commit plan

1つのPR内で、次の日本語green commitへ分ける。

1. `docs: Research全体受付容量の仕様を定義`
2. `fix(backend): Research timeoutをDB終端状態へ収束`
3. `feat(backend): Research受付容量を原子的に制限`
4. `fix(queue): agent pendingの回収経路を安定化`
5. `feat(api): Research混雑時のtyped 503契約を追加`
6. `feat(frontend): Research混雑時に入力を保持して案内`
7. `feat(observability): agent容量とqueue滞留を観測`

testは各behaviorの実装commitへ同居させ、testだけのred commitを作らない。
DB schema変更と新規dependencyはない。

## Rollout / re-evaluation triggers

本sliceの実装完了はproduction deployまたはサービス再稼働の承認を意味しない。
deploy前に別途runtime設定をread-backする。

次の変更では本仕様を再評価する。

- `worker-agent` Machine数またはTaskiq `--workers`を1より大きくする。
- `--max-async-tasks 10`を変更する。
- Taskiq task timeout 300秒を変更する。
- taskiqまたはtaskiq-redisのresolved versionを変更する。
- Redis major versionを変更する。
- agent Stream名、consumer group名、ACK typeを変更する。
- automatic retryを有効化する。
- `agent` Streamへ新しい長時間taskを追加する。
- queued/active上限またはRetry-Afterを変更する。
- provider fan-outまたは1run内の並列call数を増やす。
- productionで複数worker Machineを起動する。

## Done

- 本仕様のProblem、Goals、Invariants、Non-goalsを満たす。
- committed queued runが10件、queued + runningが20件を超えない。
- 複数API process相当の同時requestでもadvisory lockにより上限を守る。
- capacity拒否時にthread、message、run、quota、enqueue、provider callを残さない。
- capacity拒否がtyped 503、Retry-After 60、no-storeとしてOpenAPIへ公開される。
- 日次quota 429、same-thread 409、thread 404、generic 503の既存契約を維持する。
- capacity解放がDB terminal commitだけから導出される。
- handler内270秒timeoutがcurrent attemptをfailedへ収束させ、300秒Taskiq timeoutをbackstopにする。
- agent brokerが1件ずつ新規messageを取得し、1分wakeによりidle pendingを10〜11分目標で再配送する。
- Redis lag/pending/oldest ageが受付判断ではなくtransport監視として観測される。
- 1 worker process・1 Machine・max async 10の運用前提とscale-out再評価triggerが文書化される。
- frontendがtyped capacity 503を識別し、入力保持・自動再送なし・手動再送可の専用案内を行う。
- log、metric、error responseにuser input、PII、provider raw responseを含めない。
- backend Pydantic schema、OpenAPI、generated frontend typeが同期する。
- 対象backend/frontend testと標準checkがgreenである。
- production deploy、Fly scale変更、service再稼働は別承認のまま維持される。
