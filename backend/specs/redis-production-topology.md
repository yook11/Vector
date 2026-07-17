# Redis production topology

> 日付: 2026-07-17
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

## Greenfield analysis Stream topology

現在は非公開・未デプロイで、productionの旧Redis volume、in-flight task、legacy Streamを
引き継がない。初回deployでliveなanalysis Streamとして作るのは次の2本だけである。

| Stage | Stream | Group | Consumer | `MAXLEN` |
|---|---|---|---|---|
| curation | `pipeline:curation` | `taskiq` | `broker_analysis` | approximate `~10,000` |
| assessment | `pipeline:assessment` | `taskiq` | `broker_analysis` | approximate `~10,000` |

旧`pipeline:analysis`はproductionに存在しない構成とし、dual-read、3 Stream互換期間、
legacy migrationは不要であり、migrationを行わない。旧volumeを再利用する必要が生じた場合は
このgreenfield前提を適用せず、producer停止、drain、ACL、rollbackを含む別migrationを定義する。

`pipeline:curation`と`pipeline:assessment`は論理的には分離されるが、1つの
`broker_analysis`と1つのTaskiq worker process、共有concurrency 10でconsumeする。
したがってstage別concurrency、優先度、backpressure、process failure isolationはまだ分離しない。

## ACL boundary

Redis ACLはapp境界に合わせる。

- `core`: `~* &* +@all`を維持する。
- `collect`: `pipeline:metadata` / `pipeline:content`の既存権限に加え、
  `pipeline:curation`へのproducer権限だけを持つ。
- `collect`には`pipeline:assessment`、assessment用autoclaim lock、embedding、maintenance、
  hold / budget keyを公開しない。
- result backendの`taskiq:*`とmetadata/content用autoclaim lockは既存どおり維持する。

初回公開時の拒否側確認には`ACL DRYRUN`を使う。形式不正なraw `XADD`をproduction Streamへ
書き込む検証は行わない。

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

この値はhard upper boundではない。理由は次のとおり。

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
継続alertではない。

## Developmentとのfailure semantics差

docker-composeのbroker Redisは256 MB / `allkeys-lru`である。開発時のOOM回避を優先するため、
Stream keyもeviction対象になり得る。productionの`noeviction`はentryをevictせずwriteを拒否するため、
devとproductionはmaxmemory到達時のfailure behaviorが異なる。

devの`redis-rl`は64 MB / `volatile-ttl`で、broker Redisとは物理分離する。localに旧
`pipeline:analysis`が残っていても自動`DEL`せず、production capacityには算入しない。

## Monitoring / operator contract

`observe_pipeline_queue_health`はmaintenance workerから毎分、curation / assessmentを独立して読む。
full snapshot成功時だけretained entries、lag、pending、3種類のenqueue age、
`observation_up=1`、Redis TIME由来の`observation_timestamp`を記録する。失敗時は該当stageの
`observation_up=0`だけを更新し、直前のdata gaugeとtimestampを現在値として上書きしない。

productionのLogfireでは`service.name=vector-worker-maintenance`、
`deployment.environment.name=production`、stageで絞り、次を監視する。

1. `observation_up` sample自体が3分間ない。
2. `observation_up=0`が記録される。
3. successful `observation_timestamp`が3分間更新されない。

business queue ageのthresholdはbaseline取得後に決める。これらのLogfire alert作成とproduction export確認は
初回公開のexternal acceptance gateであり、このrepository変更だけでは作成済みとみなさない。

手動診断は`make pipeline-status`から`backend/scripts/pipeline_queue_status.py`を呼び、periodic samplerと
同じsnapshot helperを使う。通常診断はPEL全体を走査し得る`XPENDING ... IDLE`を実行しない。
pending停滞時だけ`--check-idle`を明示し、idle 600秒以上のentryが少なくとも1件あるかを確認する。
これはmaximum idle値ではない。

## 運用境界

- 本文書変更はdeploy、live ACL mutation、legacy key削除、Logfire alert作成を実行しない。
- group欠落はself-healではなくAI quota / costを伴い得るincidentとして扱う。
- replay / stale PEL recoveryの詳細は
  `backend/specs/curation-assessment-queue-separation-slice.md`の§8に従う。
- 初回公開後に戻せるのは、final ACL、2 Stream routing、multi-stream consumerを維持するrevisionだけである。

## 非目標

- DB schema、API response、認証・認可の変更
- curation / assessment worker、VM、containerの分割
- docker-composeから`redis-rl`を削除すること
- local legacy Streamの自動削除
