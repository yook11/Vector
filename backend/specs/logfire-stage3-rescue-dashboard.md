# Logfire Stage3 救済 dashboard 設計

## 位置付け

Phase 4 (PR `feature/logfire-rescue-visibility`) で導入する Logfire metric を
中核とした救済可視化 dashboard の設計書。code 側は本 PR で実装済 (counter /
histogram の record 経路)、Logfire dashboard / alert routing は staging で
1 週間の noise 量観察を経てから本書を参照して構築する。

設計思想:
- **失敗の見える化を優先** (`feedback_failure_visibility`)。stage3 救済が
  作動した事実は dashboard で即座に検知可能にする。
- **PII 隔離契約** (`feedback_structural_guarantee`): metric attribute に
  乗せる field は SystemConfig 由来の低 cardinality 固定値のみ
  (article_id / URL / 本文派生物は乗らない、capfire oracle で pin 済)。
- **pipeline_events と層分離**: 監査 SSoT は pipeline_events に残し、Logfire
  は「異常が起きた」事実の即時可視化と alert 起動に専念する。詳細 forensics
  は dashboard から trace を辿って pipeline_events に jump する運用。

## 監視対象 metric (Phase 4 PR で実装済)

### 1. `vector.curation.hold_set` (counter)

- 実体: [app/queue/helpers/stage_hold.py](../app/queue/helpers/stage_hold.py)
- 計測契機: `set_curation_hold(...)` が Redis SET に成功したとき
- attribute: `reason` (= `AIProvider*Error.CODE` または `"unknown"`、enum-like 固定値)
- 想定 reason 値: `ai_error_configuration` / `ai_error_insufficient_balance` /
  `ai_error_request_invalid` などの terminal_keep 系 CODE
- 期待 baseline: **0 / 日**。発火は provider/stage 健全性問題の発生を意味する。
- alert: 過去 1h で 1 回以上 → **warning** (terminal_keep failure 発火検知)

### 2. `vector.curation.hold_set_failed` (counter)

- 実体: [app/queue/helpers/stage_hold.py](../app/queue/helpers/stage_hold.py)
- 計測契機: `set_curation_hold(...)` の Redis SET が例外で失敗したとき
- attribute: `reason` (set 試行の reason をそのまま記録、検査軸として)
- 期待 baseline: **0 / 週**。Redis 自体の問題なので発火は事業継続リスク。
- alert: 過去 15min で 1 回以上 → **critical** (Redis 障害、hold gate が
  機能しない可能性)

### 3. `vector.curation.age_deleted` (counter)

- 実体: [app/queue/tasks/backfill.py](../app/queue/tasks/backfill.py)
- 計測契機: `backfill_curations` cron (`*/15 * * * *`) 内の年齢削除 loop が
  `deleted > 0` で完了したとき (deleted を value として add)
- attribute: `stage="curation"` (Phase 5+ で他 stage 拡張時の dimension)
- 期待 baseline: 平常 1-10 件 / 日 (上流 curation が健康なら塩漬けは少ない)
- alert: 過去 24h 合計が **100 件超** → **warning** (削除スパイク、上流
  パイプライン障害の疑い)

### 4. `vector.curation.age_delete_batch_size` (histogram)

- 実体: [app/queue/tasks/backfill.py](../app/queue/tasks/backfill.py)
- 計測契機: `_delete_aged_out_curations` の毎 cycle 終了時 (0 件 cycle も
  baseline として record)
- attribute: `stage="curation"`
- 期待分布: ほぼ全 cycle が 0、稀に 1-5 件。
- alert: p99 が **20 件超** → **warning** (1 cycle 大量削除、対象記事の急増)

## panel 構成 (Logfire dashboard UI)

| # | Panel | metric | 集計 | 表示 |
|---|---|---|---|---|
| 1 | hold_set rate | `vector.curation.hold_set` | sum / 1h sliding | sparkline + alert chip |
| 2 | hold_set_failed rate | `vector.curation.hold_set_failed` | sum / 1h sliding | sparkline + 色分け (critical 赤) |
| 3 | age_deleted daily total | `vector.curation.age_deleted` | sum / 24h | bar chart (日次) |
| 4 | age_delete_batch_size p50/p95/p99 | `vector.curation.age_delete_batch_size` | histogram quantile | line (1 cycle = 1 point) |

## trace 連動 filter

dashboard の panel から trace view に jump して根本原因解析するための定型
filter:

- **hold_set が発火した時刻周辺** → `service.name=vector-worker-analysis` +
  `name=execute/curate_content` の CONSUMER span (Phase 3 で taskiq middleware
  から自動採取) を時刻 filter で抽出 → curation 失敗時の HTTP/SQL/httpx 子 span
  を辿って root cause 解析。
- **age_deleted spike 時刻** → `service.name=vector-worker-maintenance` +
  `name=execute/backfill_curations` span を filter → 削除対象 article がどの
  cron cycle で出たかを trace で追跡。dashboard 単体では article_id が attribute
  に乗らない (PII 隔離契約) ため、詳細 forensics は pipeline_events DB query
  との併用が必要。

## alert routing (TBD、本 Phase スコープ外)

本 Phase の spec は **閾値 + panel 設計のみ**。実 routing (Slack webhook / メール)
は staging 投入後 1 週間の noise 量観察を経て次 PR で実装する。観察軸:

- hold_set: false positive 率 (実害なしの一時的 provider 障害で何回発火するか)
- hold_set_failed: 過敏すぎる閾値ではないか
- age_deleted: 平常運用での日次 delta 範囲

routing 実装時の参考: Logfire dashboard の Alerts UI で metric threshold rule を
作り、Slack incoming webhook (秘匿は `fly secrets` 経由で運用、`os.environ` 直
参照禁止のため `config.py` 経由) に飛ばす設計。

## PII 不在の構造的契約 (test oracle)

Phase 4 で追加した 4 metric について、attribute 経路に PII が混入しない事実は
test レベルで pin している:

- `tests/test_curation_hold_metrics.py` — capfire fixture で hold_set /
  hold_set_failed の record を捕捉、attribute に reason 以外 (article_id /
  URL に類する dynamic 値) が乗らないことを JSON 全文検索 oracle で検証。
- `tests/test_maintenance_age_delete_metrics.py` — capfire fixture で
  age_deleted / age_delete_batch_size の record を捕捉、attribute が
  `{"stage": "curation"}` 1 key のみで article_id が出ないことを oracle 化。

これらが落ちる = metric 経路で PII 漏出経路が新設された合図 (Logfire SaaS
への流出経路として spec 上扱う)。

## 反映後の運用フロー (運用者向け)

1. **hold_set warning 受信時**:
   - dashboard panel 2 (hold_set_failed) を併確認 → Redis 自体が健康か
   - `reason` attribute を確認 (`ai_error_configuration` なら API key /
     model 名 / endpoint misconfig、`ai_error_insufficient_balance` なら
     DeepSeek 残高、`ai_error_request_invalid` なら caller 側 bug)
   - 6h TTL 内に根本原因 (API key 投入 / 残高チャージ等) を解消すれば、
     TTL 切れ後の次 cron で自動再投入される
2. **hold_set_failed critical 受信時**:
   - Redis cluster の health check → 接続性 / メモリ枯渇 / replication 状態
   - hold が立たない = backfill 救済が provider 障害下でも fail-open で叩き
     続ける状態。budget で頭打ちはあるが、provider 側の rate limit / cost を
     圧迫しうるため迅速対応
3. **age_deleted spike warning 受信時**:
   - dashboard panel 4 (batch_size histogram p99) で「単発 cycle 大量削除」か
     「複数 cycle で連続削除」かを判定
   - 連続削除 → 上流 (Stage 1/2) パイプライン障害 (記事は来るが curation 通過
     しない状態) の可能性 → pipeline_events で curation 失敗パターンを集計
   - 単発大量 → 単一 source の品質劣化 (terminal_keep 連発) → source 別の
     curation 失敗率を pipeline_events DB query で確認
