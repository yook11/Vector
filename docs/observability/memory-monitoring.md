# Memory Monitoring & OOM Alerting

taskiq worker / scheduler のメモリ逼迫を OOM 到達前に検知するための常設監視 runbook。
本番診断 (`fly logs`) で worker / scheduler の OOM crash loop が起き、VM を右サイズしてきたが
(#712 / #714 / #717 / #722)、逼迫を**事前に**検知する仕組みが無かった。本書はその予兆監視
(フェーズ1) の構成と、確定検知 (フェーズ2) の棲み分けを記録する。

## 役割分担: Fly = 確定 / Logfire = 予兆

OOM は kill された瞬間に当該プロセスが最後の報告を出せないため、**確定検知は infra (Fly) 側**に
寄せる。一方 RSS 増加・MemAvailable 低下は生存中しか拾えないため、**予兆検知は app (Logfire) 側**で行う。

| 平面 | 信号 | 性質 | 現状 |
|---|---|---|---|
| Fly platform | `fly_instance_exit_oom == 1` (主) / `fly_instance_exit_code == 137` (補助) / `fly_instance_up` フラッピング | OOM・restart の確定事実。app が死んでも出る (lagging) | 閲覧のみ。アラートはフェーズ2 |
| Logfire | `system.memory.utilization{available}` / `process.memory.usage` | OOM の予兆。生存中しか拾えない (leading) | **フェーズ1で実装済** |

## 出すメトリクス (フェーズ1)

`backend/app/logfire_setup.py` の `setup_logfire()` が、**token 設定時のみ**
`logfire.instrument_system_metrics()` で次の 2 つを観測する (`base=None` で basic の cpu/swap は出さない)。
全プロセス (API lifespan / 各 worker WORKER_STARTUP / scheduler entrypoint / collect worker) が
`setup_logfire()` をプロセス毎に 1 度呼ぶため、各プロセスに独立した収集器が載る。

| メトリクス (OTel 標準名) | 用途 | 粒度 |
|---|---|---|
| `system.memory.utilization{state=available}` | VM 逼迫判定 (アラート対象) | VM 全体 (`/proc/meminfo` 由来) |
| `process.memory.usage` | 犯人特定 (どの worker が太ったか) | プロセス単位 |

infra 信号なので、ドメインメトリクス規約 (`vector.*` 低 cardinality) には乗せず OTel 標準名のまま出す
(将来フェーズ2の Collector 集約 / Fly Grafana 互換のため)。Fly Machine は実 VM (Firecracker) なので
`/proc/meminfo` が VM 実効値を返し、`available` をそのまま閾値にできる (cgroup limit 誤読が起きない)。

token 未設定の dev / CI / test では logfire 自体が `send_to_logfire="if-token-present"` で no-op になるため、
受け手の無い環境で 60s 周期の psutil コールバック収集器を立てないよう、観測も token gate する。

**collect (vector-collect) は別 app なので token を別途 set する。** core と collect は別 Fly app で
secret も独立しているため、collect 側に `LOGFIRE_TOKEN` を入れないと token gate により
`vector-worker-content` / `vector-worker-metadata` は emit しない (= dashboard に出ない)。core と
**同じ write token** を使えば同じ Logfire プロジェクトに集約される:

```sh
fly secrets set LOGFIRE_TOKEN=<core と同じ write token> -a vector-collect
```

#715 の起動時 fail-fast 検証が効くため、形式 (`pylf_v1_<region>_…`) の正しい write token を入れること
(壊れた token は起動を止める)。

## Fly process group ↔ Logfire service_name

service 名は `worker_service_name(label)` = `vector-worker-{label}` (`backend/app/queue/lifecycle.py`)。
label は broker 単位であり、**Fly process group 名 (insights / fetch) とは一致しない**。

| Fly process group (app) | Logfire service_name | VM |
|---|---|---|
| `api` (vector-core, scale-to-zero) | `vector-api` | 単独 |
| `scheduler` (vector-core) | `vector-scheduler` | 単独 |
| `worker-analysis` (vector-core) | `vector-worker-analysis` / `vector-worker-embedding` / `vector-worker-maintenance` | 同居 |
| `worker-insights` (vector-core) | `vector-worker-trend_discovery` / `vector-worker-briefing` | 同居 |
| `worker-fetch` (vector-collect) | `vector-worker-metadata` / `vector-worker-content` | 同居 |

## Logfire アラート (VM 逼迫)

`system.memory.utilization{available}` は VM 全体値で、同居 service が**同一値を複数行**出す。
`GROUP BY service_name` では同 VM が複数行返るため、CASE で **Fly process group 単位**に畳んで
1 VM = 1 行に落とす (Slack ノイズ抑制)。

```sql
SELECT
  CASE service_name
    WHEN 'vector-worker-analysis'        THEN 'worker-analysis'
    WHEN 'vector-worker-embedding'       THEN 'worker-analysis'
    WHEN 'vector-worker-maintenance'     THEN 'worker-analysis'
    WHEN 'vector-worker-trend_discovery' THEN 'worker-insights'
    WHEN 'vector-worker-briefing'        THEN 'worker-insights'
    WHEN 'vector-worker-metadata'        THEN 'worker-fetch'
    WHEN 'vector-worker-content'         THEN 'worker-fetch'
    WHEN 'vector-scheduler'              THEN 'scheduler'
    WHEN 'vector-api'                    THEN 'api'
    ELSE service_name
  END AS fly_process_group,
  min(scalar_value) AS min_available
FROM metrics
WHERE metric_name = 'system.memory.utilization'
  AND attributes->>'state' = 'available'
  AND recorded_timestamp > now() - interval '5 minutes'
GROUP BY fly_process_group
HAVING min(scalar_value) < 0.20
ORDER BY min_available ASC
```

- 評価間隔: 5 分
- 通知モード: *starts or stops having results* (逼迫の発生と解消の両方)
- 通知先: Slack webhook

CASE は topology (service → group) を SQL に焼くため、worker を別 process group へ移したら本 map も
更新する (本書が単一の出所)。将来 metrics に VM 識別 resource attribute (`service.instance.id` 等) が
乗れば topology 非依存にできる。

## ダッシュボード (Logfire)

Logfire の Dashboard に SQL-backed タイルを 3 枚置く (Explore のアドホックと違い永続化される)。
時系列タイルは **service を SQL 側で列に展開 (ピボット)** する — そうすると各列名がそのまま凡例の
系列名になり、UI の "Split by" 設定に依存せず確実に名前付きの線になる (非ピボットだと凡例が
`service_name` / `scalar_value` の列名のまま出て読めない)。バケットは `date_bin` で 1 分に丸める。

### タイル1: VM available% (process group ごと / line / 参照線 20)

VM 逼迫の俯瞰。同居 service は同値なので process group 単位に畳む。`×100` で % 表示。

```sql
SELECT
  date_bin(interval '1 minute', recorded_timestamp, timestamp '1970-01-01') AS t,
  min(CASE WHEN service_name IN ('vector-worker-analysis','vector-worker-embedding','vector-worker-maintenance') THEN scalar_value END) * 100 AS worker_analysis,
  min(CASE WHEN service_name IN ('vector-worker-trend_discovery','vector-worker-briefing') THEN scalar_value END) * 100 AS worker_insights,
  min(CASE WHEN service_name IN ('vector-worker-content','vector-worker-metadata') THEN scalar_value END) * 100 AS worker_fetch,
  min(CASE WHEN service_name = 'vector-scheduler' THEN scalar_value END) * 100 AS scheduler,
  min(CASE WHEN service_name = 'vector-api' THEN scalar_value END) * 100 AS api
FROM metrics
WHERE metric_name = 'system.memory.utilization'
  AND attributes->>'state' = 'available'
  AND recorded_timestamp > now() - interval '6 hours'
GROUP BY t
ORDER BY t
```

### タイル2: プロセス RSS (service ごと / line / unit=bytes)

犯人特定。VM をまたぐので stack せず line (どのプロセスが太いかのランキング)。

```sql
SELECT
  date_bin(interval '1 minute', recorded_timestamp, timestamp '1970-01-01') AS t,
  max(CASE WHEN service_name = 'vector-api'                    THEN scalar_value END) AS api,
  max(CASE WHEN service_name = 'vector-scheduler'              THEN scalar_value END) AS scheduler,
  max(CASE WHEN service_name = 'vector-worker-analysis'        THEN scalar_value END) AS analysis,
  max(CASE WHEN service_name = 'vector-worker-embedding'       THEN scalar_value END) AS embedding,
  max(CASE WHEN service_name = 'vector-worker-maintenance'     THEN scalar_value END) AS maintenance,
  max(CASE WHEN service_name = 'vector-worker-trend_discovery' THEN scalar_value END) AS trend_discovery,
  max(CASE WHEN service_name = 'vector-worker-briefing'        THEN scalar_value END) AS briefing,
  max(CASE WHEN service_name = 'vector-worker-content'         THEN scalar_value END) AS content,
  max(CASE WHEN service_name = 'vector-worker-metadata'        THEN scalar_value END) AS metadata
FROM metrics
WHERE metric_name = 'process.memory.usage'
  AND recorded_timestamp > now() - interval '6 hours'
GROUP BY t
ORDER BY t
```

### タイル3: collect (worker-fetch) VM 内訳 (content/metadata / stacked area)

最優先 VM。content / metadata は同居なので stack が物理的に正しい (合計が VM の実 RSS 占有)。

```sql
SELECT
  date_bin(interval '1 minute', recorded_timestamp, timestamp '1970-01-01') AS t,
  max(CASE WHEN service_name = 'vector-worker-content'  THEN scalar_value END) AS content,
  max(CASE WHEN service_name = 'vector-worker-metadata' THEN scalar_value END) AS metadata
FROM metrics
WHERE metric_name = 'process.memory.usage'
  AND service_name IN ('vector-worker-content', 'vector-worker-metadata')
  AND recorded_timestamp > now() - interval '6 hours'
GROUP BY t
ORDER BY t
```

注意:
- 0 への瞬間的な落ち込みは実メモリ 0 でなく、その 1 分バケットにサンプルが無い (~60s emit と 1m
  バケットのズレ / worker 再起動) ときの描画。resolution を 2〜5m に上げると均される。
- ダッシュボードのタイルに独自の時間レンジが付くなら `recorded_timestamp > now() - interval '6 hours'`
  の行は外してよい (レンジの二重指定回避)。

## 運用上の読み分け・注意

- **犯人特定は別軸**。`process.memory.usage` は service 別に dashboard で見る (プロセス単位に意味が
  あるため畳まない)。アラートは VM 逼迫 (上記) のみ。
- **api の欠落は正常**。`api` は scale-to-zero のため stopped 中は metrics が出ない。上記 SQL は
  「行が返れば fire」なので、欠落を逼迫と誤検知しない (`absent` で誤発火しない)。
- **collect が最優先監視対象**。process group `worker-fetch` (= `vector-worker-content` /
  `vector-worker-metadata`、vector-collect app) は、content worker が untrusted HTML 処理で RSS が
  伸びる。現在 768mb (`backend/fly.collect.toml`) なので、閾値 (上の `0.20`) の確定は 768mb 配備後の
  実測でチューニングする。
- **副次効果**: 同じ gauge で scheduler 512mb (#722) / 各 VM の peak RSS を継続実測でき、右サイズの
  妥当性検証がそのまま回る。

## フェーズ2: Fly OOM 確定検知 (後追い)

OOM の「起きてしまった事実」の確定検知。Fly の managed Grafana (fly-metrics.net) では**アラートを
張れない** (Fly staff 明言) ため、次のいずれかが必要:

- Logfire 公式が推す形: OTel Collector を 1 つ挟み `fly_instance_exit_oom` を Logfire に取り込み、
  「RSS 上昇 → OOM → 起因タスクの trace」を 1 画面で相関する。
- もしくは self-host Grafana / Prometheus + Alertmanager を立てて Fly Prometheus
  (`https://api.fly.io/prometheus/<org>/`) を読む。

フェーズ1で予兆を取れている前提で後追いとする。新コンポーネント (Collector) が増えるため、本書では
scope 外。`fly_instance_exit_oom` は当面 `fly logs` / Fly managed Grafana で閲覧する。

## 検証

- unit: `backend/tests/test_logfire_setup.py` が「token 設定時に 2 メトリクスだけを `base=None` で 1 度
  観測」「token 未設定では観測しない (gate)」を pin する。
- 本番: deploy 済み。Logfire で 2 メトリクス × 全 service (collect 含む) の emit を確認済み。
  ダッシュボードは「ダッシュボード」節の 3 タイルで構築。アラートは「Logfire アラート」節の CASE SQL
  で手動作成する (Create alert → Slack webhook)。

## 関連

- `backend/app/logfire_setup.py` — `setup_logfire()` (観測の登録点)
- `backend/app/queue/lifecycle.py` — `worker_service_name()` / 各 worker の `setup_logfire()` 呼出
- [pipeline-events-design.md](pipeline-events-design.md) — 監査 SSoT (本書の telemetry とは別層)
