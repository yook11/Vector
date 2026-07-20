# Agent user daily request quota slice 仕様

## 位置付け

Vector の research agent は、認証済みユーザーの質問を user message と queued run として
Postgres に保存し、commit 後に worker へ enqueue する。

本 slice は、ユーザーごとの research agent リクエストを日本時間の1日10件までに制限する。
利用可否の判定と枠予約は backend の Postgres を正本とし、run 作成と同じ transaction で
原子的に行う。

ここで制限するのは、JST暦日に受理されたrunの総作成数ではなく、queued cancelでまだ解除されて
いない予約数である。queued cancel後は同じ日に再利用できるため、1日のsubmit総数、run row作成数、
queue task投入数が10件以下になることは保証しない。

2026-07-20 のユーザー合意:

- 上限は認証済みユーザー1人につき、JST暦日で10件とする。
- DB上の `used_count + 1` は、run 作成時点では「消費確定」ではなく「受付時の枠予約」とする。
- queued 中にキャンセルできた場合は予約を解除し、同日の利用数として数えない。
- worker が `queued -> running` を確定した後は、キャンセルしても返却しない。
- 利用可否、枠予約、user message、run 作成を同じ Postgres transaction で確定する。
- BFF、Redis、worker、AnsweringRunner は利用可否判定の正本にしない。
- v1 では enqueue、provider、生成処理の失敗による自動返却を行わない。
- reset 境界は `Asia/Tokyo` の0:00とする。
- 1つの PR で実装し、仕様、DB、backend、API、frontend、observability を日本語コミットに分ける。

前提仕様:

- `backend/specs/agent-threads-runs-boundary-slice.md`
- `backend/specs/agent-attempt-epoch-fencing-token-slice.md`
- `backend/specs/agent-answering-runner-boundary-slice.md`
- `backend/specs/agent-live-stream-transport-slice.md`

## Problem

現在の research agent には、認証済みユーザーが同じJST暦日に保持できる未返却予約数の上限がない。

既存の制限機構は今回の利用枠と責務が異なる。

- frontend/BFF の rate limit は、session/IP 単位の短時間の連打・DoS 緩和である。
- `ProviderRateLimitGate` は、provider/model 単位の外部 API quota 保護である。
- backfill の daily budget は、batch 投入量を role 単位で制御する。

また、次の方式では正しい日次利用枠を保証できない。

- `agent_runs` の当日件数を数える方式は、thread 削除による FK CASCADE で履歴が消えると
  利用枠が復活する。
- 利用可否を事前 `SELECT` し、後から run を作成する方式は、同時 request が同じ残枠を
  観測して10件を超えて受理し得る。
- worker の `queued -> running` 時に初めて加算する方式は、API が上限超過 request も202で
  受理し、message、run、queue task を作成した後で非同期に拒否することになる。
- API transaction を worker 開始まで保持する方式は、未commit runをworkerが読めず、
  queue待ち中にconnectionとlockを保持するため成立しない。

queued 中のキャンセルを返却対象にする場合、worker 取得との競合を事前 `SELECT` の status で
判定すると、running へ進んだ run の枠を誤って返却し得る。返却可否は、DB上の条件付き
status transition の勝者で決める必要がある。

また、worker停止またはqueue滞留が20分を超えると、AI処理を開始していないqueued runも次回の
stale sweepで `failed/stale` となる。v1ではstaleによる自動返却を行わないため、その後にcancelしても
予約を解除できず、同じJST暦日の枠は翌日resetまで回復しない。障害中に10件受理されてすべてstaleに
なると、AI処理が一度も始まっていなくても、そのユーザーは当日の新しいrequestを送れない。この
利用者影響を受容する代わりに、quota対象queued runのstale発生を運用検知できなければならない。

## Evidence

- `app/agent/router.py::create_research_response()` は user message と queued run を同一
  transaction で保存し、commit 後に enqueue する。
- `app/agent/runs/repository.py::create_user_run()` は thread の user 所有権、active run、
  user message、run 作成を1つの command として扱う。
- `app/queue/tasks/agent_run.py::run_agent_answer()` は処理の先頭で run attempt を取得する。
- `app/agent/runs/repository.py::acquire_for_execution()` は active run を running へ遷移させ、
  `attempt_epoch` を増加させる。
- worker の再配送では同じ run を再取得し得るが、新しい run row は作成しない。
- `app/agent/runs/repository.py::cancel_run_for_user()` は user 所有権を確認して active run を
  cancelled terminal へ遷移させる。
- `app/agent/runs/repository.py::sweep_stale_runs()` は queued/running runを20分で
  `failed/stale` へ遷移させ、schedulerは10分間隔で実行する。現行logはsweep総件数だけを記録し、
  queued/running、quota対象/legacyの内訳を持たない。
- `app/models/agent_run.py` は thread 削除時に run が CASCADE 削除される構造である。
- `AnsweringRunner` は回答workflowを所有し、DB、SQLAlchemy、Taskiq、Redis、transactionを
  知らない境界として定義されている。
- production Redis は永続化されているが、run 作成 transaction には参加できない。
  Redis で先に加算すると、DB rollback時に枠だけが残る二重store不整合を生む。

## Goal

1. 同一ユーザー・同一JST暦日の未返却予約数を最大10件に制限する。
2. 10件目までを202で受理し、11件目は同期的な typed 429 で拒否する。
3. 利用可否、枠予約、user message、run 作成を同じ DB transaction で確定する。
4. 上限超過時は thread、message、run を残さず、enqueue と AI 処理を起動しない。
5. 正常なcounterを持つrunでは、worker取得前の queued キャンセルだけが予約を1件解除する。
6. retry、再配送、複数provider callで二重予約しない。
7. 日跨ぎ、同時request、cancel/acquire競合でも利用数を一意に確定する。
8. backendのtyped errorをfrontendで安全に識別し、専用文言を表示する。
9. quota判断と返却の結果を、本文を含まないlogと低cardinality metricで観測できるようにする。
10. quota対象queued runがstaleとなり、未開始のまま予約が回復不能になる障害を検知できるようにする。

## 用語と責務境界

### 日次利用枠

認証済みユーザーが1つのJST暦日に保持できる、未返却のresearch agent request予約数。
上限は正のdomain定数10件で固定する。設定値、role、plan、request、repositoryのpublic引数から
変更できる値にはしない。

### `usage_date`

枠予約UPSERTの `statement_timestamp()` を `Asia/Tokyo` へ変換した暦日。枠予約statementの
開始を日付確定の線形化点とする。worker開始日、完了日、cancel受付日、transaction commit日から
再計算しない。

### 枠予約

run 作成 transaction 内で日次counterを1件増やすこと。transaction がcommitした場合だけ
予約成立とする。run insertまたはcommitが失敗した場合、counter更新もrollbackする。

### 予約解除

quota対象runを `status = queued` から cancelled terminal へ遷移できた場合に、同じ
transactionで元の `usage_date` のcounterを1件減らすこと。

### 返却不可境界

worker の条件付き更新が `queued -> running` を確定した時点。実際のprovider HTTP call開始を
境界にはしない。

### 利用数

```text
JST usage_dateに属し、run作成transactionがcommitしたquota対象runの予約数
- worker取得前のqueued cancelで解除された予約数
```

accepted requestやcancelの事実はlog上に残るが、queued cancelで解除されたrunは日次利用数には
含めない。この利用数はgross submit数、run row作成数、queue task投入数の上限を表さない。

### Admission commandとtransaction owner

既存のrepository境界に合わせ、`AgentRunRepository.create_user_run()` をuser run admissionの
唯一のproduction commandとする。このcommandが、thread所有権、active run、日次枠予約、
user message、run作成を1つのcontractとして保証する。

HTTP routerは既存どおり `session.begin()` でtransaction scopeを所有し、認証済み `user_id` と
requestをadmission commandへ渡してtyped domain errorをHTTPへ変換する。routerがquotaの
事前SELECTや独立incrementを組み立てない。repositoryはcommitせず、callerのtransactionへ参加する。

cancelは `AgentRunRepository.cancel_run_for_user()` を唯一のproduction commandとし、runの
user所有権、queued/running transition、queued時の予約解除を同じcontractで扱う。別entry pointを
追加する場合も、quota queryやrun insertを個別に呼ばず、このadmission/cancel commandを再利用する。

本sliceでは新しいapplication serviceまたはUnit of Work abstractionを追加しない。既存の
AgentRun repositoryがtable単位ではなくrun lifecycle command単位でtransactional invariantを
所有する境界を維持する。

## 全体フロー

```text
POST /api/v1/research/responses
  ├─ Pydantic request validation
  ├─ authentication
  ├─ agent configuration check
  └─ DB transaction A
       ├─ existing threadのuser所有権確認
       ├─ active run競合確認
       ├─ JST usage_dateを1回確定
       ├─ 日次枠を条件付きUPSERTで1件予約
       │    └─ 予約不能 -> rollback -> typed 429
       ├─ user message作成
       ├─ AgentRun(status=queued, quota_usage_date=usage_date)作成
       └─ commit
            ├─ admission log / metric
            ├─ Taskiqへrun_idだけenqueue
            └─ 202 {threadId, runId}

worker
  ├─ DB transaction B
  │    ├─ queued/running runを取得
  │    ├─ status=running
  │    └─ attempt_epoch + 1
  ├─ terminalなら冪等skip
  ├─ bounded history取得
  ├─ AnsweringRunner.run()
  │    ├─ question context
  │    ├─ planning
  │    ├─ retrieval
  │    └─ answer generation
  └─ DB transaction C
       └─ completedまたはfailedを永続化

POST /api/v1/research/runs/{run_id}/cancel
  └─ DB transaction
       ├─ runのuser所有権確認
       ├─ status=queued限定のcancelを試行
       │    ├─ quota対象runなら元usage_dateの予約を1件解除
       │    └─ legacy/non-quota runなら予約変更なし
       ├─ queued cancelが不成立ならstatus=running限定のcancelを試行
       │    └─ 予約解除なし
       └─ commit
            └─ 必要なterminal eventをpublish
```

## Invariants

- 同一 `(user_id, usage_date)` の `used_count` は0以上10以下である。
- 日次上限は正の固定domain定数 `10` とし、quota queryのcallerが任意のlimitを渡せない。
- 利用可否判定とincrementを別queryへ分けない。
- quota予約、user message、runは同じtransactionでcommitまたはrollbackする。
- user起点のrun作成は `create_user_run()` admission commandを迂回しない。
- quotaの事前確認、increment、run insertを別々のproduction APIとして公開しない。
- quota拒否時は新規thread、message、runを残さない。
- quota拒否時はenqueue、AnsweringRunner、providerを起動しない。
- quota DB判定不能時はfail-closedとし、runを作成しない。
- 1つのquota対象runが予約する枠は最大1件である。
- Taskiq再配送、running再取得、`attempt_epoch`増加では追加予約しない。
- provider内部retryや1 run内の複数LLM callでは追加予約しない。
- 正常なcounterを持つquota対象runでは、queued cancelの予約解除は最大1回である。
- 予約解除は `cancel_run_for_user()` 内でqueued限定UPDATEが成功した分岐だけが所有する。
- 汎用failed遷移、`mark_failed()`、`mark_enqueue_failed()`、`sweep_stale_runs()`、
  `complete_run()`、ORM eventから予約解除を呼ばない。
- terminal後のstatusまたはerror codeを見て、過去にqueuedだったと推測して予約解除しない。
- counter欠損または0件というinvariant違反時は、cancelを優先し、解除不能を観測する。
- running、completed、failed、stale、enqueue_failedのrunは予約解除しない。
- thread削除は予約解除理由にしない。
- cancel/acquire競合は、事前に読んだstatusではなく条件付きUPDATEの結果で決める。
- `attempt_epoch == 0`だけをqueued cancelの根拠にしない。
- 前日runの翌日queued cancelは前日のcounterだけを減算し、当日枠へcreditを付けない。
- BFF、Redis、log、metricをquota判定のSSoTにしない。
- question、history、provider raw responseをquota log、metric、error responseへ含めない。

## Non-goals

- rolling 24時間の利用制限。
- ユーザーごとのlocal timezone対応。
- role、plan、課金状態ごとの上限変更。
- 上限を0にした一時停止、設定値による上限変更、上限10以外への動的変更。
- administratorによる手動加算・返却。
- 残り回数表示用のGET API。
- submit前のfrontend/BFFによる権威的なpreflight判定。
- token数、provider call数、tool call数による課金。
- enqueue失敗、provider失敗、generation失敗、stale時の自動返却。
- thread削除時の返却。
- Idempotency-KeyによるHTTP POST再送の重複排除。
- Redis counterまたはPostgres/Redis二重書込み。
- run単位の完全なquota ledger。
- 1日のgross submit数、run row作成数、queue task投入数を10件以下にするhard cap。
- queued submit/cancel連打そのものを日次quotaで防ぐこと。
- 実provider requestの直前まで返却可能にする新しい `starting` / `llm_started` 状態。
- 過去の日次counterを削除するretention job。

短時間のsubmit/cancel連打は既存BFF rate limitの責務とし、本日次枠へ混在させない。browserから
到達するresearch submit/cancelの両Server Actionは、既存application rate limiterでmutationとして
評価され、通常のsession/IP bucketを必ず通ることをrelease前提とする。このbucketはagent専用でも
mutation専用でもなく、既存のreadを含むrequestと共有する。

ただし、このBFF rate limitはagent専用のgross submission上限ではなく、Redis障害時には
fail-openする。通常時も許可rate内でsubmit/cancelを反復すればrun rowとqueue taskは増加するため、
DB増加量を厳密に制限する防御にはならない。この残存リスクはv1で受容する。hard capが必要になった
場合は、返却可能な日次利用枠とは別概念のgross submission制限を別sliceで設計する。

## 設計判断

### 1. 正本はPostgresの日次counterとする

専用table `agent_user_daily_quotas` を追加する。

| column | type | contract |
|---|---|---|
| `user_id` | UUID | `auth.user.id`へのFK、user削除時CASCADE |
| `usage_date` | DATE | `Asia/Tokyo`の暦日 |
| `used_count` | INTEGER NOT NULL | 未返却予約数、0以上10以下 |

主キーは `(user_id, usage_date)` とする。利用可否queryはこの完全キーを使用するため、追加indexは
設けない。counterが0になってもrowを削除せず、delete/recreate競合を作らない。

新規tableの作成migrationには、runtime roleが利用するための明示GRANTを含める。

### 2. runに元の予約日を結びつける

`agent_runs.quota_usage_date DATE NULL` を追加する。

- quota導入後に枠を予約したuser runは、同じtransactionで `usage_date` を設定する。
- 導入前の既存runは `NULL` のままとし、counterをbackfillしない。
- `NULL` runをqueued cancelしてもcounterを減算しない。
- 値は「元の予約日」を表し、予約解除後も履歴上の相関情報として保持する。
- worker開始日やcancel日で上書きしない。

このmarkerがない場合、デプロイ前に作成されたqueued runをデプロイ後にcancelした際、同日の
counterを誤って減算し得る。また、日跨ぎcancelの返却対象日を安全に特定できない。

### 3. 利用可否と予約は条件付きUPSERT一文で確定する

概念query:

```sql
WITH quota_clock AS MATERIALIZED (
  SELECT statement_timestamp() AS observed_at
),
quota_day AS MATERIALIZED (
  SELECT
    observed_at,
    (observed_at AT TIME ZONE 'Asia/Tokyo')::date AS usage_date
  FROM quota_clock
),
reservation AS (
  INSERT INTO agent_user_daily_quotas (
    user_id,
    usage_date,
    used_count
  )
  SELECT :user_id, usage_date, 1
  FROM quota_day
  ON CONFLICT (user_id, usage_date)
  DO UPDATE
  SET used_count = agent_user_daily_quotas.used_count + 1
  WHERE agent_user_daily_quotas.used_count < :daily_limit
  RETURNING used_count
)
SELECT
  quota_day.observed_at,
  quota_day.usage_date,
  clock_timestamp() AS decided_at,
  reservation.used_count
FROM quota_day
LEFT JOIN reservation ON TRUE;
```

`:daily_limit` は backend 内部の正の固定domain定数 `10` をparameter bindingする。quota queryの
public APIやcallerへlimit引数を公開せず、configまたはrequestから上書きしない。INSERT側が
`used_count = 1` を無条件に作るのは、この定数が常に1以上であるというdomain invariantに基づく。
外側のSELECTは常に1行を返し、`reservation.used_count IS NULL` なら上限到達、整数なら予約成功とする。
返された `usage_date` を同じtransactionで `AgentRun.quota_usage_date` へ設定する。`decided_at` は
row lock待ちを含むquota判断完了時のDB時刻であり、429の `Retry-After` 計算へ使用する。
quota repository/query内で `commit()` しない。

将来0または可変上限を扱う場合は、このSQLのINSERT側guardだけを局所変更して済ませない。domain定数、
DB CHECK、INSERT/UPDATE条件、429の `limit` schema、UI文言、testを同じ別sliceで改訂する。

同一user・同一日に11 requestが並行しても、row lockと条件付きUPDATEにより10件だけが
整数の `reservation.used_count` を得る。異なるuserまたは異なる日は独立したrowを更新する。

### 4. エラー優先順位を既存契約に合わせる

入力検証・認証・既存構成確認は現在のAPI境界を維持する。existing threadへのrequestでは、
thread所有権とactive run競合をquota予約より先に確定する。

| condition | HTTP | quota |
|---|---:|---|
| unauthenticated | 401 | 変更なし |
| blank / type / length invalid | 422 | 変更なし |
| agent configuration unavailable | 503 | 変更なし |
| thread not found / not owned | 404 | 変更なし |
| active run conflict | 409 | 変更なし |
| daily limit reached | 429 | 変更なし |
| accepted | 202 | 1件予約 |

quota queryまたはtransactionが予期せず失敗した場合は5xxへ収束し、allowへfallbackしない。

### 5. workerはquotaを再判定・再加算しない

`quota_usage_date IS NOT NULL` のrunは、API transactionで枠予約済みであることをpreconditionとする。
導入前またはrolling deploy中に旧applicationが作成したlegacy runは `quota_usage_date IS NULL` のまま
workerへ到達でき、既存どおり実行する。workerはいずれのrunでもquotaを再判定・再加算しない。
`acquire_for_execution()` は persistent run lifecycleとattempt fencingだけを所有する。

workerでincrementすると、running runの再取得やTaskiq再配送で二重予約する余地が生じる。
APIで単なるprecheckだけを行ってworkerでincrementする構成も、複数queued runを202で過剰受理する
ため採用しない。

### 6. queued cancelとworker acquireはDB transitionで競合させる

初期状態がqueuedのとき、cancelとworker acquireのどちらが先にDB更新を成立させたかで結果を
決める。

| winning transition | run result | quota |
|---|---|---|
| cancel: `queued -> failed/cancelled` | terminal | 元利用日から1件解除 |
| worker: `queued -> running` | execution開始 | 返却不可 |
| cancel: `running -> failed/cancelled` | terminal | 変更なし |

cancel処理は、最初に `status = queued` 限定UPDATEを実行する。成功したquota対象runだけ、同じ
transactionでcounterを減算する。queued更新が0行なら `status = running` 限定UPDATEへ進み、
成功してもcounterは変更しない。

counter減算queryは `cancel_run_for_user()` のqueued成功分岐からだけ呼ぶprivateな実装詳細とする。
`queued -> failed` という遷移結果だけでは解除理由を識別できないため、汎用status transition hookへ
減算を置かない。特に `mark_failed()`、`mark_enqueue_failed()`、`sweep_stale_runs()`、
`complete_run()` はcounterへ書き込まない。cancel処理の後段で `status = failed` または
`error_code = cancelled` を再読して解除する構成も採用しない。

```sql
UPDATE agent_user_daily_quotas
SET used_count = used_count - 1
WHERE user_id = :user_id
  AND usage_date = :quota_usage_date
  AND used_count > 0
RETURNING used_count;
```

cancel APIが再送されても、最初のrequestでrunはterminalになっているためqueued限定UPDATEは
再度成功せず、返却は最大1回となる。

quota対象queued runに対応するcounter rowが欠損、または `used_count = 0` で減算できない場合、
underflowは行わない。ユーザーの停止要求を優先してcancel自体はcommitし、固定reasonのerror logと
`inconsistent` metricを記録する。通常経路ではこの状態をDB・repository testで到達不能にする。

### 7. 日付は枠予約statement開始時のJST暦日に固定する

`usage_date` は枠予約UPSERTの `statement_timestamp()` から求め、counterとrunへ同じ値を使用する。
`statement_timestamp()` はstatement開始時に固定されるため、counter rowのlock待ち中にJST 0:00を
跨いでも開始時の日付へ計上する。transaction開始時刻、application clock、commit時刻を日付決定に
混在させない。

CIで実時刻の0:00を待たず境界を検証できるよう、quota SQL builder内部のclock expressionには
test seamを設ける。production admission commandはclock引数を公開せず、常に
`statement_timestamp()` を使用する。testだけが固定 `TIMESTAMPTZ` expressionへ差し替え、JST境界の
投影を検証する。

transactionが最終的にrollbackした場合、予約と `quota_usage_date` はどちらも残らない。commitが
0:00以降になっても、commit済みrunの `quota_usage_date` はstatement開始時の日付を維持する。

例:

```text
2026-07-20 23:58 JST  run作成 -> 7月20日のcounterを予約
2026-07-21 00:02 JST  queued cancel
  -> 7月20日のcounterを1件解除
  -> 7月21日のcounterは変更しない
```

日次resetのための更新jobは作らない。日付が変わると新しい主キーのrowを利用する。

### 8. enqueue以降の失敗では予約を解除しない

run作成transactionがcommitした後は、次の結果でも予約を維持する。

- enqueue失敗。
- enqueue失敗をrunへ記録する第2transactionの失敗。
- worker crash、stale sweep。
- provider timeout、rate limit、schema不正、generation失敗。
- input safetyによる固定拒否回答。
- running後のcancel。
- thread削除。

enqueue失敗時に通常の失敗記録まで成功した場合は、既存どおりrunを `failed/enqueue_failed` とする。
失敗記録まで失敗してAPIが503になっても、最初のrun/quota transactionがcommit済みなら予約は
残る。v1では外部副作用に応じたcompensation transactionを追加しない。

stale sweepはqueuedとrunningの両方を対象にする。quota対象queued runがworker取得前のまま20分を
超過し、次の10分間隔sweepで `failed/stale` になってもcounterは変更しない。terminal化した後の
cancelは新しいqueued transitionを成立させられないため、予約は同じJST暦日のresetまで回復不能となる。
これはv1で意図的に受容するfailure policyであり、sweepまたはcancelの実装漏れとして自動補償しない。

このpolicyは、次の3つの `queued -> failed` を同一に扱うことを意味しない。

| transition owner | error code | quota |
|---|---|---|
| user cancel command | `cancelled` | queued限定UPDATEの勝者だけ1件解除 |
| enqueue失敗記録 | `enqueue_failed` | 予約維持 |
| stale sweep | `stale` | 予約維持 |

障害時に未開始runの予約維持が量産されることはobservabilityで検知し、返却policyの変更が必要になった
場合はcompensationを原子的に設計する別sliceで扱う。

### 9. HTTP POST再送は別runなら別予約とする

clientが202 responseを受け取れず同じ質問を再送しても、v1ではIdempotency-Keyを扱わない。
別runがcommitされた場合は別の1件として予約する。既存threadでactive runと競合すれば409となり、
そのrequestは予約しない。

### 10. quota導入前とrolling deploy中のrunはlegacyとして扱う

migrationをapplicationより先に適用する。migration時点では既存runをcounterへbackfillせず、
`quota_usage_date = NULL` のまま維持する。

quota-aware applicationがcommitしたrunだけがcounterを予約し、`quota_usage_date` を持つ。
rolling deploy中に旧application instanceが作成したrunもlegacy扱いとなり、queued cancel時に
counterを減算しない。

cutoverは次の順序に固定する。

1. migrationを適用し、旧applicationとも互換なnullable columnとquota tableを先に用意する。
2. create/cancelを処理する全application instanceをquota-aware versionへ更新する。
3. 旧writer instanceが0件になったことを確認する。
4. queued stale用のproduction Logfire alertを作成し、critical通知の到達を確認する。
5. その後に到来する最初のJST 0:00を、日次10件保証の開始時刻とする。

混在期間は旧instance経由のrequestを計上できず、同じ日の導入前requestもbackfillしないため、日次10件の
保証期間に含めない。既存runへの推測backfillや、created_atだけを根拠にした返却は行わない。

applicationをpre-quota versionへrollbackする場合、migrationは残してschema互換性を維持する。
rollback開始時点でquota保証を停止し、再度全writerをquota-aware versionへ揃えた後の最初のJST 0:00
から保証を再開する。mixed versionやrollback期間のcounterを完全な利用数として扱わない。

## API Contract

対象endpoint:

```text
POST /api/v1/research/responses
```

成功時の既存契約は変更しない。

```json
{
  "threadId": "00000000-0000-0000-0000-000000000000",
  "runId": "00000000-0000-0000-0000-000000000000"
}
```

日次上限到達時は `429 Too Many Requests` と typed bodyを返す。

```json
{
  "detail": "Daily research request limit exceeded",
  "code": "research_daily_request_limit_exceeded",
  "limit": 10,
  "resetAt": "2026-07-21T00:00:00+09:00"
}
```

Pydantic SSoT:

```python
class ResearchDailyRequestLimitExceededResponse(_CamelBase):
    detail: Literal["Daily research request limit exceeded"]
    code: Literal["research_daily_request_limit_exceeded"]
    limit: Literal[10]
    reset_at: datetime
```

`resetAt` は `usage_date` の翌日0:00 JSTを `+09:00` 付きtimezone-aware datetimeで返す。これは
自動的な日次reset時刻であり、queued予約が解除されてそれ以前に再利用可能になる可能性は表さない。

`Retry-After` はquota queryが返したPostgres `decided_at` を使い、
`max(0, ceil(resetAt - decidedAt))` の整数秒とする。枠予約statementがrow lock待ち中に
0:00を跨ぎ、`resetAt` が応答時刻以前になった場合は `0` を返す。bodyとheaderで別の基準日を
計算しない。429 responseには次を付ける。

```text
Retry-After: 次のJST 0:00までの秒数
Cache-Control: no-store
```

429 schemaはrouteの `responses` にmodelとして登録し、OpenAPI generated typeを `unknown` に
しない。flat bodyを維持し、`HTTPException(detail=dict)` による `detail` 二重nestは作らない。

このcodeはrun作成前のadmission errorであり、永続run用の `ResearchRunErrorCode` へ追加しない。
既存のBFF短時間rate limitやSSE connection limitが返す汎用429には、このcodeを付けない。

## Frontend / BFF Contract

BFFはquotaを判定せず、backendのtyped 429をfrontendで扱える形へ変換する。

共通API error正規化は、少なくともHTTP status、typed bodyの `code`、`limit`、`resetAt` と、
`Retry-After` headerを失わないruntime narrowingを持つ。HTTP statusだけで分岐すると既存の汎用429と
混同するため、次をすべて確認する。

```text
status == 429
code == "research_daily_request_limit_exceeded"
limit == 10
resetAtがtimezone-aware datetimeとして解釈可能
```

`Retry-After` はtyped日次上限の識別条件にしない。有効な非負整数ならbackendがPostgres時刻から
算出した値を `retryAfterSeconds` として優先する。headerが欠損または不正でもtyped bodyが有効なら、
BFF server時刻を `now` として次を計算し、typed日次上限へ変換する。

```text
retryAfterSeconds = max(0, ceil((resetAt - now).total_seconds()))
```

このfallbackはUI案内の継続性だけを担い、quota判定には使用しない。backend/BFF間のclock skewにより
秒数が厳密でない可能性を許容する。runtime narrowingのtestではclockを注入可能にするが、productionの
submit actionはbrowser時刻を信頼しない。typed body自体が欠損または不正な429だけをgeneric errorへ
fallbackする。

research submit actionの結果はdiscriminated unionとする。

```typescript
type SubmitResearchQuestionResult =
  | { kind: "accepted"; run: ResearchRunStartResponse }
  | {
      kind: "daily-request-limit-exceeded";
      resetAt: string;
      retryAfterSeconds: number;
    };
```

UI contract:

- `daily-request-limit-exceeded` では入力内容を消さない。
- redirect、thread refresh、revalidateを行わない。
- `retryAfterSeconds > 0` では「本日の利用上限（10回）に達しました。未開始のリクエストを停止すると、
  その分を再度利用できます。利用枠は日本時間の翌日0:00にリセットされます」と表示する。
- `retryAfterSeconds == 0` では「利用枠がリセットされました。もう一度お試しください」と表示する。
- backendの `detail` をそのままユーザー向け文言として表示しない。
- codeなし・未知codeの429は既存のgeneric error表示へfallbackする。
- codeが一致しても `limit` または `resetAt` が不正な429はgeneric error表示へfallbackする。
- v1では残り件数、日次counter、countdownを画面へ常時表示しない。

## 消費・返却マトリクス

| condition | quota effect |
|---|---:|
| authentication失敗 | 変更なし |
| Pydantic validation失敗 | 変更なし |
| agent configuration不備 | 変更なし |
| thread不存在・他user所有 | 変更なし |
| active run競合 | 変更なし |
| 日次上限超過 | 変更なし |
| quota予約後のrun insert/commit失敗 | transaction rollback |
| run作成transaction commit成功 | 1件予約 |
| client切断・202 response喪失 | 予約維持 |
| enqueue失敗 | 予約維持 |
| Taskiq再配送・worker retry | 追加予約なし |
| provider失敗・generation失敗 | 予約維持 |
| input safety block | 予約維持 |
| quota対象queued runのstale sweep | AI未開始でも予約維持、当日中のcancel返却不可 |
| quota対象running runのstale sweep | 予約維持 |
| quota対象queued cancel | 正常時は1件解除 |
| quota対象queued cancel、counter欠損・0 | cancel成功、解除なし、inconsistent観測 |
| legacy/non-quota queued cancel | 変更なし |
| running cancel | 変更なし |
| completed/failed再cancel | 変更なし |
| thread削除 | 変更なし |

## Observability

### Structured log

次の固定event名を使用する。

| event | level | timing |
|---|---|---|
| `agent_user_daily_quota_reserved` | info | run/quota transaction commit後 |
| `agent_user_daily_quota_rejected` | info | 上限到達のrollback後 |
| `agent_user_daily_quota_released` | info | queued cancel transaction commit後 |
| `agent_user_daily_quota_release_inconsistent` | error | cancel commit後 |
| `agent_user_daily_quota_stale_reservations_retained` | warn | stale sweep transaction commit後、quota対象runを1件以上sweepした場合 |

予約・解除logには `run_id`、`usage_date`、更新後件数、limitを含めてよい。rejectedにはrunが
存在しないためtrace context、`usage_date`、limitだけを含める。通常logに `user_id`、question、
history、provider名、provider raw errorを追加しない。

transaction commit前に成功logを出さない。log書込み失敗をquota transactionへ参加させない。

stale予約維持logはbatch単位で1件とし、遷移前status別の `queued_count`、`running_count` を含める。
個別の `user_id`、`run_id`、`usage_date` は含めない。既存の `agent_runs_stale_swept` を維持する場合も、
quota障害検知は上記の固定eventとmetricをcontractとし、総sweep件数だけに依存しない。

### Metric

```text
agent_user_daily_quota_admissions_total{
  result="accepted|rejected"
}

agent_user_daily_quota_releases_total{
  result="released|not_eligible|inconsistent"
}

agent_user_daily_quota_stale_reservations_total{
  previous_status="queued|running"
}
```

metric attributeに `user_id`、`run_id`、`usage_date`、questionを含めない。
`inconsistent > 0` は運用alert対象とする。accepted/released metricはtransaction commit後に記録し、
rollback前に成功metricを加算しない。

`not_eligible` は、running runまたは `quota_usage_date = NULL` のlegacy queued runをcancelへ
遷移できたが、仕様上counterを解除しない場合に記録する。already completed/failedで新しいtransitionが
成立しない再送では記録しない。

stale metricはsweep transaction commit後、quota対象runの遷移前statusごとの件数を加算する。
running staleは同じmetricで観測するが、次のqueued障害alertとは分ける。

### Alert

quota対象queued runのstaleは、AI未開始の予約を当日中に回復不能にするため、次のLogfire alertを
production cutover前に作成する。

| field | contract |
|---|---|
| name | `agent-user-daily-quota-queued-stale` |
| service filter | `service.name=vector-worker-agent` |
| environment filter | `deployment.environment.name=production` |
| metric filter | `agent_user_daily_quota_stale_reservations_total{previous_status="queued"}` |
| aggregation window | 過去15分の増分合計 |
| threshold | 1件以上 |
| severity | critical |
| resolve | 過去15分の増分合計が0件へ戻った場合 |

単発でもユーザーの当日枠を回復不能にするため、初期閾値は大量発生まで待たず1件とする。notification
routingはproductionのcritical運用通知先へ接続し、release operatorがtest notificationの受信を確認する。
通知先が未設定またはtest notificationが届かない状態では、日次10件保証のcutoverを開始しない。

alert発火時は、agent worker、agent queue、schedulerの稼働と直近のqueued stale件数を確認する。
このalertからcounterの手動減算、自動返却、runの再実行は行わない。返却または復旧操作を追加する場合は
別sliceで原子性と利用者通知を定義する。

Postgres counterが利用可否のSSoTであり、logとmetricは判定やreconciliationの入力にしない。
run単位の長期監査ledgerが必要になった場合は別sliceで扱う。

## Tests

### Migration / model

1. `agent_user_daily_quotas` のcolumn、composite PK、FK CASCADE、CHECKを検証する。
2. `agent_runs.quota_usage_date` がnullable DATEであることを検証する。
3. runtime roleのGRANTをupgrade/downgradeで検証する。
4. counterの `used_count IS NULL`、`used_count < 0`、`used_count > 10` をDBが拒否する。

### Quota query

1. 新規user/dateの初回予約が `used_count = 1` を返す。
2. 10件目まで成功し、11件目は `reservation.used_count IS NULL` となる。
3. 別user、別日付のcounterは独立する。
4. run insert失敗、transaction rollbackでincrementが残らない。
5. counterが0でもrowを保持する。
6. DB障害時にallowへfallbackしない。
7. production quota queryのcallerがlimitを渡せず、固定domain定数、DB CHECK上限、429の
   `limit: 10` が一致する。

### Concurrency

競合testは実Postgresに対して独立したsession/transactionを使用し、barrierで更新順序を制御する。
同じsession内の逐次呼出しを並行性の証明にしない。

1. 同一user・同一日に11 transactionを同時開始し、run 10件、counter 10、拒否1件になる。
2. `used_count = 10` でqueued releaseが先にcommitした場合、新規reserveが成功し最終countが10になる。
3. `used_count = 10` で新規reserveの拒否が先に線形化した場合、そのrequestは429となり、後続release後の
   最終countが9になる。
4. queued cancelがacquireに勝つ場合、runはfailed/cancelled、counterは1減少、attemptは取得されず、
   Runnerは起動しない。
5. acquireがqueued cancelに勝つ場合、attemptが取得されcounterは不変となり、その後のrunning cancelでも
   解除しない。
6. queued cancelの二重送信で解除は1回だけとなる。
7. queued cancelがstale sweepに勝つ場合だけ1件解除し、stale sweepが勝つ場合は解除せず、後続cancelでも
   counterが変わらない。
8. queued cancelがenqueue失敗記録に勝つ場合だけ1件解除し、enqueue失敗記録が勝つ場合は解除せず、
   後続cancelでもcounterが変わらない。
9. cancel、stale、enqueue_failed、completeの競合で二重解除またはunderflowしない。
10. 実Postgresの短いrow lock待ちで、同一statementの `observed_at` が待機前後で変化しないことを
   検証する。
11. DB clock test seamへ23:59台と0:00以降の固定 `TIMESTAMPTZ` を与え、前日・翌日のusage_dateへ
   分かれることを検証する。CIで実際のJST 0:00を待たない。

### API / run integration

1. 10件目は202、11件目はtyped 429を返す。
2. 429時にthread、message、run、enqueueが発生しない。
3. 404、409、422、agent configuration不備などrun transaction前の503でcounterが変わらない。
4. run transaction commit後のenqueue失敗ではcounterが残る。
5. generated API typeで429が `ResearchDailyRequestLimitExceededResponse` になる。
6. error bodyに内部例外、SQL、user inputを含めない。
7. 429 bodyは `detail`、`code`、`limit`、`resetAt` のflat shapeで、`detail`を二重nestしない。
8. `resetAt` はusage_date翌日0:00の `+09:00` datetimeである。
9. `Retry-After` は同じresetAtとquota queryの `decided_at` からceilした非負整数である。
10. midnightを跨いでresetAtが過去になった場合、`Retry-After: 0` となる。
11. `Cache-Control: no-store` を返す。
12. run/quota commit後、enqueue失敗の記録まで失敗して503となる経路では、作成済みrunとcounterが
    残る。

### Cancel / date boundary

1. quota対象queued runは元利用日のcounterを1件解除する。
2. running、completed、failed runは解除しない。
3. `quota_usage_date = NULL` の既存runは解除しない。
4. 前日runの翌日queued cancelは前日だけを減算する。
5. 23:59台と0:00以降のrunが別counterを使用する。
6. counter欠損・0件時はunderflowせず、cancel成功とinconsistent観測を保証する。
7. 他userのcancelはrunとcounterを変更しない。
8. thread削除はcounterを変更しない。
9. quota対象queued runへの `mark_enqueue_failed()` はrunだけをterminal化し、counterを変更しない。
10. quota対象old queued/running runへの `sweep_stale_runs()` はrunだけをterminal化し、counterを
    変更しない。
11. quota対象active runへの汎用 `mark_failed()` はcounterを変更しない。
12. stale/enqueue_failed terminal後のcancelは新しいtransitionを成立させず、counterを変更しない。

### Retry / worker

1. running再取得と `attempt_epoch` 増加でcounterが変わらない。
2. terminal runの再配送はRunnerを起動せず、counterも変えない。
3. 1 run内の複数provider callとprovider retryでcounterが変わらない。
4. 202 response喪失後に同じactive threadへPOSTを再送すると409となり、counterは増えない。
5. 202 response喪失後に新規threadとしてPOSTを再送し、別runがcommitされるとcounterは1件増える。
6. worker停止を模した10件のquota対象queued runをstale sweepすると、すべて `failed/stale` になっても
   counterは10のままとなり、後続requestは429、後続cancelでも回復しない。

### Rollout

1. migration前に作成された `quota_usage_date = NULL` runをworkerが既存どおり実行できる。
2. legacy runをqueued cancelしてもcounterを減算しない。
3. quota-aware runをpre-quota schemaへ書き込む経路がなく、migration-firstの互換性を維持する。
4. queued stale alertのrule作成とcritical通知到達を、保証開始前のexternal acceptance gateに含める。
5. 旧writerが0件になったことと、次のJST 0:00から保証開始するcutover手順をrelease確認項目に含める。
6. pre-quota applicationへrollbackしてもschemaを維持し、quota保証停止を明示できる。

### Frontend

1. status、code、limit、resetAtが有効なtyped 429だけを `daily-request-limit-exceeded` へ変換する。
2. 有効な `Retry-After` headerを失わず、`retryAfterSeconds`へ変換する。
3. `Retry-After` が欠損または不正でも、有効なtyped bodyなら固定BFF時刻とresetAtから秒数を計算し、
   専用結果へ変換する。
4. generic 429、未知code、不正limit、不正resetAtは専用文言へ変換しない。
5. backendの429 responseをmockしたBFF統合testで、API error interceptorからsubmit actionの
   discriminated unionまでbodyとheaderが保持される。
6. 日次上限時に入力を保持し、redirect/revalidateしない。
7. `retryAfterSeconds > 0` では上限到達とJST 0:00の固定文言を表示する。
8. `retryAfterSeconds == 0` では即時再試行の固定文言を表示する。
9. backend detailを直接表示しない。
10. accepted時の既存clear、redirect、revalidate契約を維持する。

### BFF anti-abuse boundary

1. research submitとcancelの両Server Action transportが、SSE除外ではなく通常mutation rate limitを
   通過する。
2. 同じsession/IPのsubmitとcancelが、他requestとも共有する同じ既存bucketを消費し、上限到達時は
   Server Actionまたはbackend dispatchより前に汎用429となる。
3. BFF rate-limit Redis障害時は既存どおりfail-openし、既存のfailure logを記録する。
4. BFF rate limitが正常でも、日次quotaがgross submit数、run row数、queue task数を10以下にする
   hard capではないことをcontract testの前提から外さない。

### Observability

1. accepted、rejected、released、not_eligible、inconsistentのmetricを各1回だけ記録する。
2. rollback時にaccepted/released metricを記録しない。
3. log、metricにuser inputと高cardinality metric attributeを含めない。
4. quota対象queued/running runのstale件数を遷移前status別に、sweep commit後だけ加算する。
5. quota対象runが0件のsweepではstale予約維持logを出さず、1件以上ならbatch集約logを1件だけ出す。
6. quota対象queued staleのmetricが `service.name=vector-worker-agent` から、固定metric名と
   `previous_status="queued"` を使ってexportされ、alertが
   `deployment.environment.name=production` で絞り込める。

## Implementation scope

### Backend

- quota table/modelと `agent_runs.quota_usage_date` のAlembic migration。
- 日次枠の条件付き予約・解除queryとtyped domain result。
- run作成transactionへのquota予約配線。
- queued/running cancel transitionの分離とqueued時の予約解除。
- 429 Pydantic schema、OpenAPI response、header。
- structured log、quota対象staleのstatus別集計metric。

### Frontend

- backend Pydantic/OpenAPIからのgenerated type同期。
- typed API errorのruntime narrowingと `Retry-After` 欠損時のresetAt fallback。
- submit actionのdiscriminated union。
- research composerの専用表示と回帰test。
- research submit/cancel Server Actionが既存BFF limiterでmutationとして評価される統合test。

### Production acceptance

- release operatorがLogfireに `agent-user-daily-quota-queued-stale` alertを作成する。
- `service.name=vector-worker-agent` かつ `deployment.environment.name=production` のtest metric、または
  Logfireのtest notification機能でcritical通知の到達を確認する。
- alert ruleと通知経路の確認は外部acceptance gateであり、application transactionまたはquota判定には
  参加させない。

### Unchanged boundaries

- AnsweringRunnerのpublic contractとDB非依存性。
- Taskiq task名とrun_idだけのpayload。
- provider rate limit、backfill budget、BFF短時間rate limit。
- runの公開status `queued | running | completed | failed`。
- cancel endpointの204/404/409成功・失敗契約。
- SSE terminal event schema。

## Commit plan

1つのPR内で、次のgreenな日本語コミットに分ける。

1. `docs: ユーザー日次利用枠の仕様を定義`
2. `feat(db): 日次利用枠カウンタとrun予約日のスキーマを追加`
3. `feat(backend): 日次利用枠の原子的な予約と解除を実装`
4. `feat(api): 日次上限のtyped 429契約を追加`
5. `feat(frontend): 日次上限到達時の表示を追加`
6. `feat(observability): 利用枠のログとメトリクスを追加`

testは作業順として先に書くが、testだけのred commitは作らず、各behaviorの実装commitへ対応testを
同居させる。migration/model testはDB commit、quota/concurrency/cancel testはbackend commit、
API contract testはAPI commit、UI testはfrontend commit、metric/log testはobservability commitに含める。

## Done

- 本仕様のProblem、Invariants、Non-goalsを満たす。
- 10件目まで202、11件目がtyped 429となる。
- 同時requestでも同一user・同一日の未返却予約が10件を超えない。
- 429時にthread、message、run、enqueue、Runner、providerが起動しない。
- run作成とquota予約が同じtransactionでcommitまたはrollbackする。
- 正常なcounterを持つrunでは、queued cancelだけが元利用日の予約を最大1件解除する。
- 予約解除をcancel commandのqueued成功分岐だけが所有し、enqueue失敗、stale、汎用failed遷移が
  counterへ書き込まない。
- counter欠損・0件の異常時はunderflowせず、cancel成功とinconsistent観測へ収束する。
- worker取得後のcancel、retry、enqueue/provider失敗、thread削除では解除しない。
- worker未取得のquota対象queued runがstaleになった場合も予約を維持し、当日中に回復不能となる
  failure policyと、その発生を検知するlog、metricが実装される。
- production cutover前に、15分窓でquota対象queued staleを1件から検知するcritical Logfire alertが
  作成され、release operatorが通知到達を確認する。
- 日跨ぎと既存run切替で誤った日付・counterを減算しない。
- 枠予約statement開始時刻をusage_dateの線形化点とし、midnightを跨ぐlock待ちの結果が固定される。
- 日次上限が正の固定domain定数10であり、DB CHECKとAPI `limit` が一致し、callerから変更できない。
- backend Pydanticが429契約のSSoTとなり、generated frontend typeへ同期される。
- frontendがtyped日次上限とgeneric 429を区別し、`Retry-After` 欠損時もresetAtから復元して入力を
  保持した固定文言を表示する。
- research submit/cancelの両Server Actionが既存BFF limiterでmutationとして評価され、同時に
  その共有limiterがgross run作成数のhard capではない残存リスクが文書化される。
- logとmetricがcommit後の結果を観測し、user inputや高cardinality metric属性を含まない。
- migration-first、旧writer drain、次のJST 0:00からの保証開始、rollback時の保証停止がrelease手順に
  含まれる。
- 1つのPR内で、本仕様のCommit planに従う日本語のgreen commitへ分割される。
- migration、backend、frontendの対象testと標準checkがgreenである。
