# 監査スコープ整理 — dispatch / backfill の throughput を metric へ移設

Status: Implemented (PR #776)
Date: 2026-06-09

## Problem

`pipeline_events` が「その時点でしか分からない失敗のなぜ」(moment-only) に加えて、
run summary / throughput snapshot (occurrence) を焼いており、metric / log と役割が
重複している。最優先の dispatch / backfill から run summary / throughput snapshot を
撤去し、監査を failure-why 中心へ寄せる。per-item の成功 occurrence
(`backfill_item_enqueued` / dispatch `source_dispatched`) の整理は本 spec の射程だが、
admin consumer の移設が絡むため段階的に行う (下記スコープ参照)。

## 原則

- 監査 = which / why（消えると取れない失敗文脈: source_id・error_class・error_chain・
  provider reason）。高 cardinality 可。
- metric = rate / trend / 生死（集計値）。低 cardinality 必須。
- consumer-driven: その行 / 値に依存する問い・対応が実在しないなら焼かない。実在するなら、
  撤去ではなく consumer の移設を伴う。

## Evidence（現状）

- backfill: `backfill_run_completed`（成功 run サマリ）と run 件数 snapshot を監査に焼く。
  同値が Logfire metric (`vector.backfill.dispatched` / `backlog` / `held` / `aged_out`,
  `backend/app/queue/tasks/backfill.py:110-129`) に既存 = 二重出力。
- dispatch: `source_dispatched`（per-source 成功 occurrence,
  `backend/app/queue/tasks/acquisition.py:128`）と `dispatch_run_no_targets`（全ゼロ件数,
  `acquisition.py:81`）を焼く。成功 throughput の metric は不在（成功サマリは structlog
  `dispatch_sources_completed` のみ）。dispatch stage の succeeded outcome は
  `source_dispatched` 1 種のみ（`dispatch.py:26`）。
- Pipeline Health (admin) は succeeded / failed の **行の存在** を読む。payload は読まない。
  - `event_counts_24h`（`backend/app/admin/pipeline_health/repository.py:28`）:
    `(stage, event_type) -> count`、SUCCEEDED / FAILED のみ・24h 窓。
  - `last_succeeded_at`（`repository.py:47`）: `stage -> max(occurred_at)`、SUCCEEDED のみ。
  - backfill 健全性は別途 `backfill_stats`（backlog 件数, `repository.py:68`）が SSoT。

---

## PR1 — backfill: run summary / throughput 撤去（replacement は既存 metric）

### 撤去

- `backfill_run_completed` event（成功 run サマリ）
- `BackfillPayload` の throughput field: `selected_count` / `granted_count` /
  `enqueued_count` / `failed_count` / `limit`

### 維持 (KEEP)

- skip 制御 event の `outcome_code`（`backfill_run_kill_switch_disabled` /
  `backfill_run_held_by_stage_hold` / `backfill_run_daily_budget_exhausted` /
  `backfill_run_no_targets`）= 「なぜ走らなかったか」
- `backfill_run_failed` の `error_*`、item 単位 `backfill_item_enqueue_failed`、
  aged-out rejected 群
- `backfill_item_enqueued`（per-item 成功）= admin の succeeded 行 consumer のため当面維持
  （per-item occurrence の整理は follow-up）
- `daily_max`: `backfill_run_daily_budget_exhausted` event でのみ KEEP。これは throughput
  でなく「停止の閾値」= B 級 config snapshot（deploy 切替で消える「なぜ止まったか」の補助）。
  他 event の `daily_max` と全 event の `limit` は撤去。

### Pipeline Health への影響（受け入れる意味変更）

- backfill stage の succeeded 集計が「run 完了 + item 成功」→「**item 成功のみ**」に変わる。
  - `succeededEventCount24h`: run 1 本分が減るが item 成功が支配的。意味はむしろ明確化
    （実 enqueue 量を表す）。
  - `lastSucceededAt`: backlog が空で run が何も enqueue しない健全 idle 期間は stale に
    なりうる。backfill 健全性の SSoT は `backfill_stats`（backlog）なので許容する。

DB 影響: なし（JSONB payload field の不使用化のみ。CHECK / 列変更なし）。

---

## PR2 — dispatch: 成功 throughput を metric へ移設

### 撤去

- `source_dispatched` 成功行の append（`acquisition.py:128`）と
  `DispatchOutcomeCode.SOURCE_DISPATCHED`
- `dispatch_run_no_targets` 監査行（`acquisition.py:81`）→ heartbeat metric へ
- `DispatchPayload` の件数 field: `selected_count` / `dispatched_count` /
  `rejected_count` / `failed_count`

### 追加（admin consumer の移設）

- `dispatch_run_completed`（per-run 成功 heartbeat 行、**件数 payload なし**）を成功パス
  （1 件以上 dispatch した run）で append。N 件の per-source occurrence を 1 件の per-run
  heartbeat に畳む。admin の `lastSucceededAt[dispatch]` / succeeded 集計を維持する。
  - 決定点: 本 heartbeat 行を採用する（代替 = 行を足さず admin で dispatch を failure-only
    にする案。ただし健全な dispatch が「成功ゼロ」に見え failure-visibility を損なうため不採用）。

### 維持 (KEEP)

- `source_enqueue_failed`（which source + error_*）
- REJECTED 行（which source + `raw_source_name` + reason）
- `dispatch_run_failed`（run crash の error_*）

### 新設 metric 1: `vector.dispatch.outcome`（per-source 結果カウンタ）

| attribute | 値 |
|---|---|
| `cadence` | `high` / `medium` / `low` / `all` |
| `result` | `dispatched` / `enqueue_failed` / `rejected` |
| `reason` | `dispatched`→`none`（失敗理由なし） / `enqueue_failed`→`unclassified`（理由は監査に存在・metric 非分類） / `rejected`→`source_not_registered` \| `source_name_invalid` |

- 計上: ループ後に per-run batch add（成功 `dispatched_count`、失敗 `failed_count`、rejected は
  reason 別に集計）。`if count:` で 0 を弾く。
- `reason` の語彙: `SourceDispatchRejectionCode`（`backend/app/collection/sources/dispatch.py:31`,
  2 値 closed）+ `none` / `unclassified` の closed set のみ。
- series 上限 = `cadence(4) × [dispatched:none, enqueue_failed:unclassified, rejected:{2}] = 16`。
- 引ける問い: 成功 throughput = `result=dispatched`、成功率 =
  `dispatched / (dispatched + enqueue_failed)`、棄却傾向 = `result=rejected` を `reason` で group。

### 新設 metric 2: `vector.dispatch.run`（per-run の結末）

| attribute | 値 |
|---|---|
| `cadence` | `high` / `medium` / `low` / `all` |
| `outcome` | `target_selection_failed` / `no_targets` / `succeeded` / `partial_failed` / `all_failed` |

- `outcome` は run の **結末** を表す（「ループが return に到達した」ではない）。target 母集団
  （= 実際に enqueue を試みた source）に対して算出し、棄却は母集団外:
  - `target_selection_failed`: selection 自体が例外（対象一覧すら作れない）。
  - `no_targets`: targets 空。**全 active source が棄却された run もここに畳む**（全滅は
    `vector.dispatch.outcome{result=rejected}` で可視）。
  - `succeeded`: `dispatched == len(targets)`（全件成功）。
  - `partial_failed`: `0 < dispatched < len(targets)`（一部成功）。
  - `all_failed`: `dispatched == 0`（targets≥1 で全 enqueue 失敗。broker 全断などを healthy に
    見せない）。
- 棄却を outcome に混ぜない: enqueue 失敗＝一過性 infra 信号、棄却＝恒常的 config/カタログ品質の
  信号で対応が別。混ぜると未登録 source 1 行で毎 run `partial_failed` 化し infra 信号が鈍る。
- 計上: run 終端で +1。series 上限 = `cadence(4) × outcome(5) = 20`。
- liveness: `rate==0` → 未実行 / `no_targets` → 動いたが 0 件（idle） / `all_failed` /
  `partial_failed` → 全/一部失敗 / `target_selection_failed` → 選択クラッシュ。
- 監査 heartbeat 行（`dispatch_run_completed`, `dispatched_count>=1`）は
  `outcome ∈ {succeeded, partial_failed}` と一致し、admin lastSucceededAt と整合する。

### Pipeline Health への影響（受け入れる意味変更）

- dispatch stage の succeeded が `source_dispatched`（per-source）→ `dispatch_run_completed`
  （per-run）に変わる。
  - `succeededEventCount24h[dispatch]`: 「dispatch した source 数」→「成功した run 数」。
  - `lastSucceededAt[dispatch]`: 「最後に source を配信した時刻」→「最後に dispatch run が
    完了した時刻」。liveness としてはむしろ適切。
- `dispatch_run_no_targets` は SKIPPED で元々 admin 非対象（SUCCEEDED/FAILED のみ読む）。
  撤去による admin 影響なし。throughput / 生死は Logfire 2 metric へ。

DB 影響: なし（Logfire metric は schema 非依存。payload field 不使用化のみ）。

---

## Invariants（守る制約）

- 失敗の forensic（`error_class` / `error_message` / `error_chain` / `retryability` /
  provider `reason`）は監査から減らさない。
- metric attribute は closed 語彙のみ。**例外の生詳細を `reason` に入れない**
  （`unclassified` 止まり、詳細は監査 `source_enqueue_failed`）。
- metric は rate / trend / 生死、監査は which / why。両者が同一事実を持つ場合は consumer
  （集計 vs forensic、Logfire alert vs admin DB read）が別であること。
- per-source 粒度（`vector.dispatch.outcome`）と per-run 粒度（`vector.dispatch.run` /
  `dispatch_run_completed`）を 1 instrument / 1 行種に混ぜない。
- consumer のある succeeded 行（admin が読む）は、撤去せず最小 heartbeat に畳んで移設する。

## Non-goals

- 失敗・棄却・`*_run_failed` 監査行の撤去。
- `backfill_item_enqueued`（per-item 成功）の沈黙化と、それに伴う admin lastSucceededAt の
  再設計。本 2 PR の対象外、follow-up。
- source 別など `vector.dispatch.outcome` を超える追加 breakdown の metric 化。
- その他レビュー指摘（dead field: completion `scraper_class` / assessment `failure_action`、
  DERIVABLE: curation `input_content_length` / assessment `investor_take` 等、idempotent skip）。

## Done / 検証

- PR1 / PR2 とも `/check`（ruff + pytest）green。dispatch / backfill task・audit repository・
  admin pipeline_health の test を更新（succeeded 集計の意味変更を test に反映）。
- metric の test は `get_metrics_data` を 1 回読み None=0 扱い（capfire、Counter は二度読み不可）。
- migration なし。

## 保証するべき条件
- backfill の成功 run summary は audit に焼かれない。
- backfill の失敗・skip・item enqueue failed の forensic 情報は減らない。
- dispatch の成功 occurrence は audit に焼かれず、metric に出る。
- dispatch の失敗・rejected は audit に残る。
- dispatch run の結末（succeeded / partial_failed / all_failed / no_targets /
  target_selection_failed）が `vector.dispatch.run{outcome}` で区別できる。全 enqueue 失敗を
  succeeded/completed に見せない。
- dispatch の enqueue_failed / rejected の件数が `vector.dispatch.outcome` に正しい値で出る。
- metric attributes に source_id/source_name/error_message/error_class が混ざらない。
- 既存の古い JSONB payload は Pydantic で読める。extra="ignore" 前提の後方互換。

## 参照

- [`pipeline-events-failure-attribute-projection.md`](./pipeline-events-failure-attribute-projection.md)
  （outcome_code / retryability / failure_kind契約）
- [`pipeline-events-audit-stage-ssot.md`](./pipeline-events-audit-stage-ssot.md)
  （stage所有権の契約）
- [`acquisition.py`](../../backend/app/queue/tasks/acquisition.py)
  （throughputとdispatch outcomeの観測先）
