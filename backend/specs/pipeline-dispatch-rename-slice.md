# pipeline:dispatch 全面改名仕様

> 日付: 2026-07-18(同日レビュー反映で改訂)
>
> ステータス: 実装完了・local 検証 green(`broker_content` 系の同時改名も採用)
>
> 検証: Ruff lint / format、non-integration 3,834 件、integration 877 件(19 skip)、最終 ACL integration 2 件が green。local では `pipeline:dispatch` group と dispatch / collection program、cron → dispatch → acquisition chain を確認
>
> 対象: `pipeline:metadata` 系の命名(stream key / broker object / supervisord program / lifecycle label / Logfire service 名 / scheduler object)を `dispatch` 語彙へ、`broker_content` 系の命名(object / program / lifecycle label / service 名)を `collection` 語彙へ全面統一する
>
> 前提: acquisition / completion 分割 slice(`acquisition-completion-queue-separation-slice.md`)の実装完了・検証 green の上に積む。greenfield(未デプロイ)前提は同 slice と共通
>
> 位置付け: task / business semantics を変えず、識別子(Redis key / object 名 / service 名 / log event 名)だけを breaking rename する。§8 は将来の初回 deploy メモであり、本 slice の Done に含まない

## 1. 位置付け

`pipeline:metadata` は「RSS/HN メタデータ取得」の名残であり、実態は source dispatch・completion poller・lease sweep を担う**制御キュー**である。また `broker_content` 系の `content` は、`pipeline:content` の消滅により指示対象を失った語になっている(実態は acquisition / completion の 2 Stream を読む collection BC の共有 consumer)。分割 slice では説明(docstring / comment)だけを実態に合わせた。本 slice は名前そのものを役割語 `dispatch` / `collection` へ統一する。

deploy 後の stream key 改名は consumer group 再作成と ACL 移行を伴う migration になる。未デプロイの今は純粋なコード変更で済む最後の窓であり、release phase の直前に実施する。

改名は「同じ概念の名称変更」であり、新旧名の併存期間・互換層・dual-read を作らない。task / business semantics(何がいつどう実行されるか)は不変だが、Redis key・Logfire service 名・log event 名は外部から観測できる識別子の breaking change である。「挙動を一切変えない」とは表現しない。

## 2. Work Definition

### 2.1 Problem

- stream key `pipeline:metadata`、broker object `broker_metadata`、supervisord program `metadata`、lifecycle label `"metadata"`(→ Logfire service / asyncpg application_name `vector-worker-metadata`)、scheduler object `scheduler_metadata` が、いずれも実態(dispatch 制御)と乖離した語を使っている。
- `broker_content` / `[program:content]` / lifecycle label `"content"`(→ `vector-worker-content`)は、参照先の `pipeline:content` が分割 slice で消滅したため指示対象を失っている。
- 分割 slice 完了時点で説明文は修正済みだが、名前表面は残っている。「stream は dispatch / acquisition / completion、object は metadata / content」という新旧混在語彙を解消する。

### 2.2 Evidence(改名表面の実測 2026-07-18)

| 名前表面 | 所在 |
|---|---|
| `pipeline:metadata`(stream key) | `brokers.py` / `infra/redis/fly.toml`(ACL 2 pattern)/ `fetch.conf` comment / `fly.collect.toml` comment / `docs/architecture.md` / `redis-production-topology.md` / tests(operator contract、collect ACL integration) |
| `broker_metadata`(object) | `brokers.py`(2)/ `lifecycle.py`(5)/ `schedulers.py`(3)/ `tasks/acquisition.py`(5: import + decorator ×4)/ `tasks/completion.py`(3: import + decorator ×2)/ `fetch.conf` command / `fly.collect.toml`(2)/ `docker-compose.yml` comment(「broker 名は無変更」の設計注記)/ tests(test_brokers ×5、test_brokers_otel_middleware ×2、operator contract ×2) |
| `[program:metadata]`(supervisord) | `fetch.conf` / test_brokers の `_fetch_worker_commands` 期待 dict key |
| lifecycle label `"metadata"` | `lifecycle.py` の `WORKER_POOL_SIZING` key / `_register_worker_lifecycle` / `_register_scheduler_lifecycle`。`worker_service_name` 経由で Logfire service 名と asyncpg application_name(`vector-worker-metadata`)、log event 名(`metadata_worker_startup/shutdown`、`metadata_scheduler_startup/shutdown`)に波及 |
| scheduler 統合 comment | `supervisord/scheduler.conf`(「4 つの cron scheduler (metadata / ...)」— agent 追加後の現在は 5 scheduler であり件数も stale) |
| `broker_{metadata,content}`(ブレース表記 comment) | `tests/test_lazy_ai_sdk_import.py`。token grep をすり抜ける形のため gate 設計の根拠になる |
| `scheduler_metadata`(object) | `schedulers.py` / `scheduler_entrypoint.py`(2)/ tests(test_scheduler_routing ×2、test_brokers ×3) |
| `autoclaim:taskiq:pipeline:metadata`(派生 lock key) | `infra/redis/fly.toml` / tests(operator contract、ACL integration) |
| `vector-worker-metadata`(観測語彙) | `logfire-stage3-rescue-dashboard.md` の filter。ただし対象の `backfill_curations` は `broker_maintenance` の task のため、この filter は**現時点で誤記**(正 = `vector-worker-maintenance`)。§4 で誤記修正として扱う |
| `broker_content`(object) | `brokers.py`(2)/ `lifecycle.py`(2)/ `tasks/acquisition.py`(2)/ `tasks/completion.py`(2)/ `fly.collect.toml`(2)/ `redis-production-topology.md`(3: 4-stage 表の consumer 列ほか)/ `docs/architecture.md` / tests(test_brokers ×13、test_brokers_otel_middleware ×2、operator contract ×6、test_scheduler_routing ×2) |
| `[program:content]`(supervisord) | `fetch.conf` / test_brokers の `_fetch_worker_commands` 期待 dict key |
| lifecycle label `"content"` | `lifecycle.py` の `WORKER_POOL_SIZING` key / `_register_worker_lifecycle`。log event 名(`content_worker_startup/shutdown`)と、`vector-worker-content` の期待値(test_db_ssl / test_logfire_db_pool / test_logfire_setup / test_db_application_name)に波及 |

task name(`dispatch_high/medium/low`、`dispatch_sources`、`dispatch_html_fetch_jobs`、`sweep_expired_leases`、`acquire_source`、`scrape_html_body`)は既に役割語であり改名対象ではない。`worker-fetch` container 名と Makefile `WORKERS` も対象外とする(container は collect Fly app の「外部 fetch を担う側」を指す配置名であり、broker / stage 語彙とは別の軸)。

### 2.3 Invariants

1. task / business semantics を変えない。task name、cron schedule、timeout / retry labels、payload schema、chain、ACK 方式のすべてを維持する。識別子(Redis key / service 名 / log event 名)は breaking rename であり、旧名との互換を残さない。
2. broker 設定値(`maxlen=10_000` / `idle_timeout=600_000` / consumer group `taskiq` / batch 100)を維持する。
3. worker program 数(2)、各 program の worker process 数(1)、concurrency cap(dispatch 10 / collection 5)、DB pool sizing 値 `(5, 5)` ×2、scheduler 統合 1 process を維持する。
4. acquisition / completion / curation / assessment / embedding ほか他 Stream の topology に触れない。
5. collect ACL の pattern 数と command surface(`resetchannels ... -@dangerous`)を維持し、変わるのはキー名のみとする。
6. 新旧名の併存を作らない。`pipeline:metadata` への producer / consumer / ACL 許可を残さない。
7. DB schema、API response、認証・認可を変更しない。

### 2.4 Non-goals

- `worker-fetch` container 名、Makefile `WORKERS` の変更
- local Redis に残る旧 `pipeline:metadata` key の自動 `DEL`(§5)
- 歴史的 spec 文書(分割 slice・curation/assessment slice 等の日付付き記録)の書き換え
- cron 時刻表、dispatch 対象選定、lease / attempt semantics などの挙動変更全般

### 2.5 Done

1. 改名 gate(二層)が成立する。旧名は仕様上、legacy 拒否テストと歴史的文書に**意図的に残る**ため、単一の zero-hit grep は成立しない。
   - **tier 1(production scope + allowlist)**: scope = `backend/app/queue` / `backend/supervisord` / `infra/redis` / `backend/fly.collect.toml` / `docker-compose.yml` / `docs/architecture.md`。`rg -nP 'broker_metadata|scheduler_metadata|metadata_(worker|scheduler)_|broker_content|content_worker_|(?<![A-Za-z0-9_])(metadata|content)(?![A-Za-z0-9_])'` を実行し、hit が `infra/redis/fly.toml` の Redis 一般用語 `group metadata` と拒否対象を説明する `legacy content` の2件だけであることを確認する。ASCII token 境界を使うため、`metadataは`のように日本語が隣接する旧語も検出し、`curate_content`は除外できる。
   - **tier 2(allowlist 付き token gate)**: repo 全体で `rg -n 'pipeline:metadata|broker_metadata|scheduler_metadata|vector-worker-metadata|program:metadata|metadata_worker_|metadata_scheduler_|broker_content|vector-worker-content|program:content|content_worker_|broker_\{[^}]*(metadata|content)[^}]*\}'` を実行し、hit の全件が許可リスト(①歴史的 spec 文書または明示的に superseded と分類された節、②本仕様自身、③旧名への**拒否**を検証するテストの assert)に分類できる。
   - gate の scope / pattern は実装時に hit を目視分類して確定し、分類根拠を PR に記録する。
2. §6 の runtime 契約テストを含む契約テスト群(broker topology / scheduler routing / operator contract / ACL integration / runtime topology / lifecycle 命名)が新名で green。
3. `/check`(unit + `make test-integration`)が green。
4. local dev で `make pipeline-restart` 後、`XINFO GROUPS pipeline:dispatch` で新 Stream / consumer group の生成を直接確認し、dispatch cron の実行ログで scheduler → dispatch worker → acquisition / completion の chain を確認する(`make pipeline-status` は 4 business stage のみを表示し dispatch を含まない)。

## 3. 改名 mapping(正本)

| 現行 | 改名後 |
|---|---|
| `pipeline:metadata` | `pipeline:dispatch` |
| `autoclaim:taskiq:pipeline:metadata` | `autoclaim:taskiq:pipeline:dispatch` |
| `broker_metadata` | `broker_dispatch` |
| `[program:metadata]` | `[program:dispatch]` |
| lifecycle label `"metadata"` | `"dispatch"` |
| `vector-worker-metadata`(Logfire service / asyncpg application_name) | `vector-worker-dispatch` |
| `metadata_worker_startup` / `_shutdown`(log event) | `dispatch_worker_startup` / `_shutdown` |
| `metadata_scheduler_startup` / `_shutdown`(log event) | `dispatch_scheduler_startup` / `_shutdown` |
| `scheduler_metadata` | `scheduler_dispatch` |
| `broker_content` | `broker_collection` |
| `[program:content]` | `[program:collection]` |
| lifecycle label `"content"` | `"collection"` |
| `vector-worker-content`(Logfire service / asyncpg application_name) | `vector-worker-collection` |
| `content_worker_startup` / `_shutdown`(log event) | `collection_worker_startup` / `_shutdown` |

`collection` 側は object / program / label / service 名のみの改名であり、stream key(`pipeline:acquisition` / `pipeline:completion`)は既に最終形のため変更しない。scheduler lifecycle は content broker に存在しないため、log event の改名は dispatch 側のみとなる。

## 4. 変更対象

| File | 変更 |
|---|---|
| `backend/app/queue/brokers.py` | broker object 名 ×2(dispatch / collection)/ stream key / docstring |
| `backend/app/queue/lifecycle.py` | pool sizing key ×2 / lifecycle 登録 3 箇所 / docstring 内の broker 列挙 |
| `backend/app/queue/schedulers.py` | scheduler object 名 / import / docstring |
| `backend/app/queue/scheduler_entrypoint.py` | scheduler import 2 箇所 |
| `backend/app/queue/tasks/acquisition.py` | broker import ×2 + decorator ×5 |
| `backend/app/queue/tasks/completion.py` | broker import ×2 + decorator ×3 |
| `backend/supervisord/fetch.conf` | program 名 ×2 / command の broker 参照 ×2 / comment |
| `infra/redis/fly.toml` | collect ACL の stream / autoclaim key 2 pattern(dispatch 側のみ) |
| `backend/fly.collect.toml` | comment の broker / Stream 参照 |
| `docker-compose.yml` | broker 設計注記と scheduler 列挙の更新 |
| `docs/architecture.md` | control Stream 名 / broker 名の更新 |
| `specs/pipeline/typed-pipeline-preconditions.md` | active phased spec 内の旧入口 task 節を superseded と明示し、現行 schedule / routing SSoT へ誘導 |
| `backend/specs/redis-production-topology.md` | topology 表(consumer 列)・ACL・capacity / recovery / 運用境界に残る rename 前提を最終形へ |
| `backend/specs/logfire-stage3-rescue-dashboard.md` | `service.name` filter を **`vector-worker-maintenance` へ修正**。`backfill_curations` は `broker_maintenance` の task であり、現行の `vector-worker-metadata` は既存の誤記。改名(dispatch への置換)ではなく誤記修正として扱う |
| `backend/supervisord/scheduler.conf` | comment の scheduler 列挙を dispatch へ更新(「4 つ」は現在 5 scheduler の stale 記述のためあわせて訂正) |
| `backend/tests/test_lazy_ai_sdk_import.py` | comment の `broker_{metadata,content}` 表記を新名へ |
| `backend/tests/test_db_application_name.py` | `worker_service_name("content")` → `"collection"` |
| `backend/tests/test_brokers.py` | broker ×2 / program dict key(dispatch / collection)/ pool・engine label / scheduler 期待値 |
| `backend/tests/test_brokers_otel_middleware.py` | broker 参照 ×2 |
| `backend/tests/test_scheduler_routing.py` | scheduler object / broker 期待集合 |
| `backend/tests/test_queue_separation_operator_contract.py` | ACL / 文書 pin の期待値(dispatch / collection 両方) |
| `backend/tests/queue/test_collect_acl_integration.py` | XADD / XREADGROUP / XACK 対象キー |
| `backend/tests/test_db_ssl.py` | `vector-worker-collection` の期待値 |
| `backend/tests/test_logfire_db_pool.py` | service / application_name 期待値 |
| `backend/tests/test_logfire_setup.py` | service 名期待値 |
| `backend/tests/test_local_runtime_topology.py` | 参照があれば更新 |

## 5. Local dev の移行

- 旧 `pipeline:metadata` key と consumer group は local Redis に残骸として残る。自動削除しない(必要なら明示的な dev cleanup で行う)。
- cron 駆動の制御 task 5 つ(`dispatch_high/medium/low`、`dispatch_html_fetch_jobs`、`sweep_expired_leases`)は耐久価値を持たず、旧 Stream の未処理 entry は失われてよい。次の cron tick が新 `pipeline:dispatch` に再生成する。
- `dispatch_sources` だけは schedule を持たない admin 明示実行専用のため、旧 entry を破棄した場合の再投入は operator の再実行による。
- completion の in-flight は DB lease が SSoT のため、旧 Stream 残骸の影響を受けない(lease 失効後に poller が再 claim する)。
- 適用は `make pipeline-restart`(scheduler / worker の fresh 再起動)で行い、`XINFO GROUPS pipeline:dispatch` で新 Stream / consumer group を、dispatch cron の実行ログで chain 動作を確認する。`make pipeline-status` は 4 business stage(acquisition / completion / curation / assessment)のみの表示で、dispatch の確認には使えない。

## 6. Tests / verification

1. **runtime 契約の直接 pin を追加する**。現状、制御 Stream のキー名を broker レベルで pin するテストは存在しない(`test_brokers.py` に `pipeline:metadata` の出現ゼロ)ため、改名時の typo を検出できない。次を契約として固定する。
   - `broker_dispatch.queue_name == "pipeline:dispatch"` と、現行 `broker_metadata` の設定維持(`consumer_group_name="taskiq"`、`consumer_id="$"`、`maxlen=10_000`、`idle_timeout=600_000`、`unacknowledged_batch_size=100`、lock timeout なし)。既定値の変更は本 slice の対象外(Invariant 1)。
   - `broker_collection` の設定が現行 `broker_content` と完全一致(primary `pipeline:acquisition` / additional `pipeline:completion` / `consumer_id="0-0"` / lock timeout 60 を含む)。正本は既存 `test_collection_broker_reads_only_stage_specific_streams` の改名更新とする。
   - lifecycle 登録が label `"dispatch"` / `"collection"` を使い、`worker_service_name` 経由の Logfire service 名と DB application_name が新名になる(test_logfire_setup / test_logfire_db_pool / test_db_ssl / test_db_application_name / test_brokers の pool・engine 契約)。
   - worker / scheduler log event が新名(`dispatch_worker_startup` / `collection_worker_startup` / `dispatch_scheduler_startup` 等)で出る。
   - `scheduler_entrypoint._SCHEDULERS` が `scheduler_dispatch` を含む 5 件の exact set である。
2. 既存契約テストの期待値を §3 mapping へ更新する(上記以外の新規テスト追加は最小限とし、正本は既存の topology / routing / operator contract テストに置く)。
3. `test_collection_control_task_keeps_metadata_routing_and_execution_contract` は名前も `..._keeps_dispatch_routing_...` へ改名し、`broker_dispatch` への登録を pin する。fetch worker program の期待 dict key は `{"dispatch", "collection"}` になる。
4. ACL integration は `pipeline:dispatch` への XADD / XREADGROUP / XACK 許可と、旧 `pipeline:metadata` への **拒否** を確認する(併存を作らない Invariant 6 の固定。この assert は旧名を恒久的に含むため、Done 1 tier 2 の許可リスト対象)。
5. Done 1 の二層 gate を実行し、hit の分類を記録する。
6. `/check`(ruff + unit + `make test-integration`)を実行する。

## 7. 実装順序

1. 契約テストの期待値を mapping へ一括更新する(Red)。
2. production code(brokers / lifecycle / schedulers / entrypoint / tasks)を改名する。
3. 設定・インフラ(fetch.conf / fly.toml ×2 / docker-compose comment)を改名する。
4. 文書(architecture / topology SSoT / dashboard 手順)を更新する。
5. 二層 gate(§2.5 Done 1)→ `/check` → local `make pipeline-restart` + `XINFO GROUPS pipeline:dispatch` で chain 動作確認。

## 8. Release phase(将来の初回 production deploy メモ — 本 slice では未検証)

**本節は本 slice の Done・実装開始条件に含まない。** 分割 slice §12.5 の合意(release 手順は rename slice 側に定義)の置き場として残す将来メモであり、内容の検証は release 実施時に行う。手順は `curation-assessment-queue-separation-slice.md` §9 を最終名で読み替えて適用する。

### 8.1 Preconditions

- 分割 slice と本 rename slice が main に merge 済みで、`/check` + integration green。
- public traffic 停止中、production に旧 Stream(`pipeline:content` / `pipeline:metadata` / `pipeline:analysis`)と旧 Redis volume が存在しない。

### 8.2 Deploy order

1. Redis ACL を最終形(`~pipeline:dispatch` / `~pipeline:acquisition` / `~pipeline:completion` / `~pipeline:curation` + 対応 autoclaim + `~taskiq:*`)へ更新する。
2. worker container(worker-fetch / worker-analysis ほか)を起動し、5 Stream(dispatch / acquisition / completion / curation / assessment)と `taskiq` group の存在を確認する。
3. scheduler 停止のまま one-shot で `observe_pipeline_queue_health.kiq()` を 3 回実行し、4 stage の `observation_up=1` と metric export を確認する。
4. collect credentials の smoke で dispatch → `acquire_source` → `curate_content` chain、および claim → `scrape_html_body` → `curate_content` chain を確認する。拒否側は `ACL DRYRUN` で旧 `pipeline:metadata` / `pipeline:content` / assessment / embedding / maintenance の `NOPERM` を確認する。
5. Redis memory・write rejection・worker restart が無いことを確認し、scheduler を起動する。
6. scheduled sample 3 回連続で freshness 3 分以内を確認してから公開する。

### 8.3 Acceptance gate

- 5 Stream + group が存在し、legacy 3 Stream が存在しない。
- collect の許可 / 拒否が §8.2-4 のとおり。
- `used_memory / maxmemory < 80%`(超過時は公開を止め capacity 対応を判断)。
- deploy 前後の worker RSS(fetch container 2 process)に回帰がない。
- smoke 中 `vector.completion.lease_swept` が 0、smoke 後に pending が 0 へ戻る。
- completion の警告閾値(oldest outstanding age >= 120 秒 warning / >= 300 秒 critical / `lease_swept > 0` critical)の alert query と runbook が用意されている。
- 最終 contract を満たす rollback-compatible image digest を記録する。それ以前の image(旧 `pipeline:metadata` / `pipeline:content` 前提)への direct rollback は禁止し、forward-fix とする(旧 collect は `NOPERM`、旧 worker は誰も書かない Stream を読むため)。

## 9. 命名の根拠

- `dispatch` は載っている 6 task 中 5 つ(`dispatch_*` ×4、`dispatch_html_fetch_jobs`)の役割そのものであり、既存の task name 語彙と一致する。
- `sweep_expired_leases` は投入ではなく lease 掃除だが、「completion の DB queue を管理する制御側の仕事」として dispatch worker に同居する説明が成立する(分割 slice §3.2 の「制御」stage)。
- より広い `control` は指示対象が曖昧になるため採らない(2026-07-18 ユーザー合意)。
- `collection` は bounded context 名(`app/collection/`)であり、「collection BC の 2 stage Stream(acquisition / completion)を読む共有 consumer」を指す。`broker_analysis`(`app/analysis/` の curation + assessment を読む)と同じ命名構図になる。

## 10. 合意事項(2026-07-18 確定)

1. dispatch 語彙への改名は stream key / broker object / program / lifecycle label / service 名 / scheduler object の全表面で行う(`control` は採らない)。
2. **`broker_content` 系の同時改名を採用する**(ユーザー確定)。§3 mapping の collection 行のとおり object / program / lifecycle label / service 名を `collection` へ改名し、stream key は変更しない。
3. 実装は本仕様のユーザー承認後に着手し、2026-07-18 に local verification まで完了した。
