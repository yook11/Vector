# Redis production topology

> 日付: 2026-07-18
>
> ステータス: 初回公開前の確定構成

## 決定

productionでは、永続ジョブを持つbroker Redisと、frontendの短命なrate-limit Redisを
別Fly appとして運用する。DB indexやfallbackだけを物理分離とはみなさない。

| Redis | 用途 | memory / eviction | persistence |
|---|---|---|---|
| `vector-redis` | Taskiq broker / result、backendの一時制御状態 | 256 MB / `noeviction` | volume + AOF |
| `vector-redis-rl` | frontend `rl:ip:*` sliding window | 64 MB / `volatile-ttl` | なし |

broker RedisのSSoTは`infra/redis/fly.toml`である。`noeviction`はtask entryを別keyの都合で
追い出さない一方、`maxmemory`到達後はwriteを拒否する。このためmemory capacityは公開前gateとし、
write rejectionを正常なbackpressureとして扱わない。

## Greenfield 4-stage Stream topology

現在は非公開・未デプロイのgreenfieldで、productionの旧Redis volume、in-flight task、
legacy Streamを引き継がない。初回deployでliveにするpipeline stage Streamは次の4本である。

| Stage | Stream | Group | Consumer | `MAXLEN` |
|---|---|---|---|---|
| acquisition | `pipeline:acquisition` | `taskiq` | `broker_collection` | approximate `~10,000` |
| completion | `pipeline:completion` | `taskiq` | `broker_collection` | approximate `~10,000` |
| curation | `pipeline:curation` | `taskiq` | `broker_analysis` | approximate `~10,000` |
| assessment | `pipeline:assessment` | `taskiq` | `broker_analysis` | approximate `~10,000` |

`pipeline:dispatch`は記事データの処理stageではなく、source dispatch、completion poller、
lease sweepを実行するcontrol Streamである。

legacy `pipeline:content`はproductionに作成せず、entryやgroupを引き継がない。dual-readや
migrationを行わない。旧volumeを再利用する必要が生じた場合は、このgreenfield前提を適用せず、
producer停止、drain、ACL、rollbackを含む別migrationを定義する。

旧`pipeline:analysis`もproductionに存在しない構成とし、dual-read、3 Stream互換期間、
legacy migrationは不要であり、migrationを行わない。

collectionの2 Streamは1つの`broker_collection`と1つのTaskiq worker process、共有concurrency 5で
consumeする。analysisの2 Streamも1つの`broker_analysis`と1つのTaskiq worker process、
共有concurrency 10でconsumeする。Stream、consumer group state、retention、lag / pending / ageは
stage別に分かれるが、stage別concurrency、DB pool、backpressure、process failure isolationは
分離しない。

## ACL boundary

Redis ACLはapp境界に合わせる。

- `core`: `~* &* +@all`を維持する。
- `collect`の許可key patternは次のexact setとする。
  - `~pipeline:dispatch`
  - `~pipeline:acquisition`
  - `~pipeline:completion`
  - `~pipeline:curation`
  - `~autoclaim:taskiq:pipeline:dispatch`
  - `~autoclaim:taskiq:pipeline:acquisition`
  - `~autoclaim:taskiq:pipeline:completion`
  - `~taskiq:*`
- `collect`のcommand surfaceは`resetchannels +@connection +@read +@write +@stream
  +@scripting -@dangerous`を維持する。
- `collect`にはlegacy `pipeline:content`、`pipeline:assessment`、`pipeline:embedding`、
  `pipeline:maintenance`と、それらのautoclaim lockを許可しない。hold / budget / rate-limit keyも
  公開しない。

このrepository変更はlive ACL mutationやdeployを行わない。初回公開時の許可・拒否確認には
`ACL DRYRUN`を使い、形式不正なraw `XADD`をproduction Streamへ書き込む検証は行わない。

## Stream healthの用語

保持量とlive group stateを同じ「queue depth」として扱わない。

| 用語 | Redis source | 意味 |
|---|---|---|
| retained entries | `XLEN` | ACK済み履歴を含め、Streamに現在保持される全entry数 |
| lag | `XINFO GROUPS` | groupへ一度も配達されていないentry数 |
| pending | `XINFO GROUPS` / PEL | 配達済みだが未ACKのentry数 |
| undelivered enqueue age | `last-delivered-id`直後の最小entry ID | 最古の未配達entryがenqueueされてからの時間 |
| pending enqueue age | PELの最小Stream ID | 最古のpending entryがenqueueされてからの時間 |
| outstanding enqueue age | 上記2 ageの最大値 | stage全体の最古enqueue age |
| pending delivery idle | PELのdelivery時刻 | enqueue ageとは異なる、stale delivery診断用の指標 |

retained entriesはACK済みentryを含むため、`XLEN`をbacklogとは呼ばない。lagとpendingも別の
状態であり、pending enqueue ageとdelivery idleも区別する。Stream / group欠落、`lag=null`、
Redis接続失敗を0へ変換しない。

## Memory / capacity trade-off

analysisの2 Streamは、それぞれapproximate `MAXLEN ~10,000`、合計約20,000 entriesの
retained history budgetを持つ。2026-07-17のlocal実測を線形換算したplanning estimateは、
2 Stream合計約9.84 MBで、旧1 Stream約4.92 MBとの比較では約4.92 MB増である。
これはstageごとに履歴とtrim budgetを分離するためのmemory trade-offである。

collectionは1 → 2 Streamへ分割され、retained history budgetが約10,000から合計約20,000
entriesになる。2026-07-18のlocal `pipeline:content`実測5,108,644 bytesを基準にすると、
追加1 Stream分は約4.9 MiBのplanning estimateである。completion payloadはint単体のため、
実値は各新Streamの`MEMORY USAGE`で補正する。

これらの値はhard upper boundではない。理由は次のとおり。

- `MAXLEN`はapproximate trimであり、entry数が一時的に指定値を超え得る。
- `queue_name` labelやpayload size、allocator、consumer group metadataの大きさが一定ではない。
- Redis 7ではpayload trim後も参照だけのghost PELが残り得る。
- ghost PELとgroup metadataは20,000 entriesのpayload budgetに拘束されない。

毎分のqueue health sampler自身も`pipeline:maintenance`へ1日1,440 entriesを追加する。
maintenance Streamの`MAXLEN ~10,000`は変更しないため、保持上限への到達とtrimの頻度は上がる。
同じlocal実測の線形換算では、maintenance Streamは10,000 entriesで約5.83 MB、観測時点から
約3.20 MB増の目安である。新2 analysis Streamと合わせた影響範囲3 Streamの最終形は
約15.67 MBであり、entry sizeとallocator overheadをdeploy前後の実測で補正する。

production broker Redisは256 MB / `noeviction`である。公開前とdeploy後に次を確認する。

- `used_memory` / `used_memory_peak` / `maxmemory`
- 各Streamの`MEMORY USAGE`とretained entries
- Redis command error / write rejection
- worker RSS、restart、fatal loop

`used_memory / maxmemory >= 80%`なら公開を止め、operatorがcapacity対応を判断する。このsliceは
Redis memoryの継続exporterを追加しないため、80%はdeploy時と手動diagnosticのcapacity gateであり、
継続alertではない。初回releaseではdeploy前後のworker RSSと上記Redis値を最終名のtopologyで
比較する。

## Developmentとのfailure semantics差

docker-composeのbroker Redisは256 MB / `allkeys-lru`である。開発時のOOM回避を優先するため、
Stream keyもeviction対象になり得る。productionの`noeviction`はentryをevictせずwriteを拒否するため、
devとproductionはmaxmemory到達時のfailure behaviorが異なる。

devの`redis-rl`は64 MB / `volatile-ttl`で、broker Redisとは物理分離する。localに旧
`pipeline:analysis`が残っていても自動`DEL`せず、production capacityには算入しない。

## Monitoring / operator contract

`observe_pipeline_queue_health`はmaintenance workerから毎分、4-stageのacquisition / completion /
curation / assessmentを独立して読む。full snapshot成功時だけretained entries、lag、pending、
3種類のenqueue age、`observation_up=1`、Redis TIME由来の`observation_timestamp`を記録する。
失敗時は該当stageの`observation_up=0`だけを更新し、直前のdata gaugeとtimestampを現在値として
上書きしない。Stream / group欠落やRedis接続失敗を0件として扱わない。

productionのLogfireでは`service.name=vector-worker-maintenance`、
`deployment.environment.name=production`、stageで絞り、次を監視する。

1. `observation_up` sample自体が3分間ない。
2. `observation_up=0`が記録される。
3. successful `observation_timestamp`が3分間更新されない。

completionはDB claim時にattemptを加算し、lease 300秒より遅れて実行されると未実行でもretry budgetを
消費し得る。このため`oldest_outstanding_enqueue_age >= 120`秒をwarning、`>= 300`秒をcriticalとする。
`vector.completion.lease_swept > 0`もlease失効が実際に起きたcritical signalである。acquisitionのageは
leaseと結合しないworker競合指標として扱い、curation / assessmentのbusiness thresholdはbaseline取得後に
決める。

これらのLogfire alert作成とproduction export確認は初回公開のexternal acceptance gateであり、
このrepository変更だけでは作成済みとみなさない。

手動診断は`make pipeline-status`から`backend/scripts/pipeline_queue_status.py`を呼び、periodic samplerと
同じsnapshot helperを使う。通常診断はPEL全体を走査し得る`XPENDING ... IDLE`を実行しない。
pending停滞時だけ`--check-idle`を明示し、idle 600秒以上のentryが少なくとも1件あるかを確認する。
これはmaximum idle値ではない。

## Admin manual fetch

`POST /api/v1/admin/pipeline/fetch`のsource ID指定経路はbest-effortであり、inactive sourceの意図的な
単発fetchを許可する。`202 Accepted`と`dispatchedCount`はenqueue受付のみで、実行、完了、耐久性を
保証しない。inactive sourceはcronで自動再投入されないため、operatorはrequest時刻とsource IDに対応する
実行証跡を確認し、queue滞留の解消後も証跡がなければ再実行する。

durable rowはdedupされるが、再実行時の外部HTTP取得と新規記事のAI処理は再発し得る。multi-sourceの
enqueueは非atomicなループで、一部だけenqueue済みになり得る。durable job ID / statusの永続化が
必要になった場合は別sliceで設計する。

## Handoff loss

acquisition / completionはDB commit後に`curate_content.kiq()`を呼ぶため、そのhandoffが失われても
元taskのtransport replayでは救済されない。回復経路は`backfill_curations`だが、無条件ではない。

- `curation:hold`中はrun全体をskipする。
- `backfill_curations_enabled` kill switchがoffなら実行しない。
- 日次予算が残る場合だけ再投入する。
- 対象は作成から30分より古く、7日より新しいchild-NULL記事に限る。

## Collection group recovery

collection group欠落時はretained historyを無条件にreplayしない。`acquire_source`は過去結果の再生ではなく
live feed再取得であり、最大約10,000 retained entriesのacquisition replayは外部HTTP burstと、その時点で
新しく見つかった記事のAI処理を発生させる。completionもDB precondition / CASで最終状態は守るが、
古いmessageによる重複HTTP実行は防げない。

復旧は次の順序で行う。

1. schedulerとworker-fetch containerを停止する。dispatch / collection両programを止め、復旧中のadmin fetchを
   禁止する。
2. acquisition / completion両Streamのretained entries、PEL、DBのopen / running状態を確認する。
3. live feed再取得、HTTP burst、重複実行、新規記事のAI costを明示し、replayを受容するかoperatorが承認する。
4. worker-fetchを再起動し、lag / pending / completion lease sweep / 外部fetch失敗を監視する。
5. 安定後にschedulerを再開し、最後に admin fetchを解禁する。

復旧のために未処理entryへ`DEL`や`XTRIM`を使わない。supervisorが先に再起動した場合は、既に一部replay
済みとして残状態を再確認する。

## 運用境界

- 本文書変更はdeploy、live ACL mutation、legacy key削除、Logfire alert作成を実行しない。
- group欠落はself-healではなくAI quota / costを伴い得るincidentとして扱う。
- analysis replay / stale PEL recoveryの詳細は
  `backend/specs/curation-assessment-queue-separation-slice.md`の§8、collection固有条件は上記runbookに従う。
- release acceptanceは最終topologyで、ACL DRYRUN、collect credentials smoke、4-stage
  sampler freshness、capacity / worker RSS gateを確認してから公開する。
- ACL cutover後のrollbackはfinal Streamを読める互換imageに限定し、そのdigestより前はforward-fixとする。

## 非目標

- DB schema、API response、認証・認可の変更
- collectionまたはcuration / assessment worker、VM、containerの分割
- completion payloadへのattempt token追加やclaim / lease / attempt semanticsの変更
- admin manual fetchのdurable job ID / status永続化
- docker-composeから`redis-rl`を削除すること
- local legacy Streamの自動`DEL` / `XTRIM`
