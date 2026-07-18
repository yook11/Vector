# 監査 skip 逃がしポリシー 仕様書

Status: Partially implemented (PR-2 completed; remaining work is tracked in Done)

pipeline_events(監査)に焼かれている「業務状態を変えない skip 系イベント」を log / metric / span へ逃がし、監査の対象を「後から調査・復旧判断・品質改善・health 表示に使う出来事」に揃える。

- 作成日: 2026-07-07
- 対象: `backend/app/`(監査基盤 `app/audit/` と各 stage の emission)
- 前提調査: 全 8 stage の監査 emission 棚卸し + consumer 契約 + 敵対的検証(完了済み)

---

## 1. Problem

pipeline_events は本来「成功・失敗・恒久的な状態変化」を後から SQL で追うための監査ログだが、現状は一部 stage で「冪等 skip / 競合敗北 / stale trigger / 運用ゲート / 対象なし」といった、業務状態を変えず集計・ログで十分追える事象まで `SKIPPED` / `REJECTED` として焼いている。同じクラスの事象が stage 間で扱いが割れており(例: 競合敗北を completion は監査に焼き、curation/assessment/embedding は log のみで揃っている)、監査ログのノイズと意味論の不整合になっている。

---

## 2. 判断基準(統一ルール)

ある emission を pipeline_events に**残す**のは、次のいずれかに該当するときだけとする。

1. 工程が実際に**成功**した(`SUCCEEDED`)。
2. 工程が実際に**失敗**した(`FAILED`)。特に**ドメイン概念の構築失敗**(VO / entity の build error、`reason_code` を持つもの)は品質改善の一次資料として必ず残す。
3. 記事 / source の**扱いが恒久的に変わる**(drop、aged_out のような打ち切り)。
4. 後から **requeue / drop / 復旧・原因調査**に使う恒久的な事実。
5. `source_health` / `pipeline_health` が**読む**。

pipeline_events から**逃がす**のは、次のように業務状態を変えず、集計・ログで十分追えるもの。

- 冪等 skip(already-processed 再投入)
- 競合に負けただけの処理(楽観ロック敗北 / URL 衝突)
- stale trigger(claim 失効後の後処理)
- 運用ゲート(kill switch / hold / no_target)
- rate limit gate(既に逃がし済み)
- 集計で十分な高頻度イベント

逃がしの機械的 discriminator: **content / ドメイン由来の `reason` を持たず、業務状態を変えないもの**。この基準は既に [completion.py:450 `_project_ready_build_error`](../../backend/app/audit/stages/completion.py) が体現しており(VO 構築失敗 → `FAILED` + `reason_code`、stale/冪等 → `SKIPPED`)、task 側 [completion.py:137](../../backend/app/queue/tasks/completion.py) も「`FAILED` のみ計上 / `SKIPPED` は log して return」とゲート済み。本ポリシーはこの既存の考え方を全 stage に広げるもので、新規の抽象化ではない。

逃がし先は **structured log + metric + span result** で十分。可観測性を落とさないことを不変条件とする(§4)。

---

## 3. Evidence(現状の consumer 契約)

pipeline_events を**クエリ時に読む consumer は 2 つだけ**。この 2 つを壊さないことが安全性の境界。

- `pipeline_health`([repository.py:31](../../backend/app/admin/pipeline_health/repository.py)): `event_type IN (SUCCEEDED, FAILED)` のみ集計。`SKIPPED` / `REJECTED` は読まない。
- `source_health`([repository.py:72](../../backend/app/admin/source_health/repository.py)): stage を `ACQUISITION` / `COMPLETION` に限定し、analyzable 成功 / `FAILED` / `REJECTED` を `outcome_code` グルーピングで読む。

帰結:
- `SKIPPED` を値として読む consumer は全 stage を通じて**ゼロ**(dead-read)。→ SKIPPED 系は逃がして UI/health 集計は壊れない。
- `REJECTED` は `acquisition` / `completion` stage のみ consumer あり。それ以外の stage の `REJECTED`(分析系 ready-build 等)も dead-read。
- 逃がしても壊れるのは emission 自体を pin する producer-side テストのみ(consumer / funnel 数値 / ダッシュボードは不変)。

`retention.py` は `occurred_at` のみ、`logfire/failure_attrs.py` は write-side で、いずれも table reader ではない。

---

## 4. Invariants(常に守る)

1. **可観測性を落とさない(ただし channel は事象の性質で決める)**: 逃がした事象は log / metric / span のいずれかで観測可能に保つ。新規 counter は原則足さない。
   - **benign な race-loss / stale / 冪等 skip / 運用ゲート(kill_switch / no_targets)**(業務状態を変えず、winner の `SUCCEEDED` が throughput を担保) → **structured log(+ 既存 gauge)のみ**。急増は log / gauge で追う。
   - **hold** は既存 gauge `vector.backfill.held` で観測(逃がし済み)。
   - 決定: completion の race-loss(`persist_superseded` / `persist_url_conflict`) / `stale_attempt` は **log のみ・counter 新設なし**。
   - **`daily_budget_exhausted` は逃がさず REJECTED として監査に残す**(§7)。理由: これは benign skip でなく「実対象(`found>0`)を予算上限で先送りした run レベルの棄却」で、`no_targets`(`found==0`=先送りなし)と質が違う。率のアラートが要る場合は Logfire 側で監査行 / log から出し、**専用 counter は作らない**。
2. **consumer 契約を壊さない**: 残す `SUCCEEDED` / `FAILED` / `acquisition・completion の REJECTED` を消さない。`pipeline_health` の succeeded/failed 集計、`source_health` の failureReasons を不変に保つ。
3. **DB schema を変えない(PR-1/1.5)**: `ck_pipeline_events_event_type` の `'skipped'`、enum、既存 pipeline_events 行を変更しない。DB からの `'skipped'` 除去・データ整理は本仕様の対象外(別判断)。
4. **processing_outcome の分母定義を変えない**: 各 stage の `record_*_processing_outcome` は「勝者のみ計上 / infra_error 分母外 / 冪等 skip・stale 非計上」を維持。
5. **ドメイン構築失敗は残す**: VO / entity build error、`reason_code` を持つ `FAILED` / `REJECTED` は監査に残す。
6. **semantic API は残す**: `append_trend_discovery_run_event_best_effort` / `_append_backfill_run_event` 等は `FAILED` / `SUCCEEDED` でも使うため関数は残し、`SKIPPED` 呼び出しだけ外す。

---

## 5. Non-goals

- DB CHECK / enum から `'skipped'` を消す(Alembic migration が必要。別判断)。
- 既存 pipeline_events データの整理・削除。
- `REJECTED` ready-build 系の意味論変更(PR-2)。
- `aged_out` の扱い変更(残す。§8)。
- 重複排除だけを目的とした逃がし helper の共通抽象化(各 stage の既存 log を使う)。

---

## 6. PR-1: SKIPPED producer をゼロにする(DB schema は触らない)

app 内で `EventType.SKIPPED` を**新規に焼く箇所を除去**する。`daily_budget_exhausted` は REJECTED への再分類が必要なため PR-1.5 に回す(PR-1 完了時点では daily_budget の 3 箇所のみ SKIPPED が残る)。

### 6.1 対象一覧(PR-1 で外す SKIPPED)

未使用になる semantic API method / helper・enum member・ClassVar は同 PR で削除する(§6.2 の Done を text-grep でなく到達可能経路で判定するため)。

| stage | call_site | outcome_code | 逃がし先(既存) | 変更 + dead-code 削除 |
|---|---|---|---|---|
| completion | [service.py:85](../../backend/app/collection/article_completion/service.py) → [completion.py:129](../../backend/app/audit/stages/completion.py) `_append_race_loss` | `persist_superseded` / `persist_url_conflict` | 既存 log(service.py:94-113 `article_completion_conflict_lost` 等)+ processing_outcome 非計上(現状維持) | `append_persist_outcome` を `SUCCEEDED` のみ emit に。**未使用になる `_append_race_loss` を削除** |
| completion | [failure_handling.py:68/147/199](../../backend/app/collection/article_completion/failure_handling.py) → [completion.py:292](../../backend/app/audit/stages/completion.py) `append_stale_attempt` | `stale_attempt` | 既存 log `article_completion_stale_attempt_ignored` | 呼び出し 3 箇所を削除。**未使用になる `append_stale_attempt` を削除** |
| completion | [completion.py:131(task)](../../backend/app/queue/tasks/completion.py) → `append_ready_build_error`(SKIPPED subset) | `...incomplete_article_missing` / `...not_running` | 既存 log `scrape_html_body_skipped`(task:141、`incomplete_article_id`+`outcome_code` 保持) | `except ArticleCompletionReadyBuildError` 側で audit を呼ばず log のみに。ReadyBuildError は全て SKIPPED 型(FAILED 型無し・確認済)なので **`_project_ready_build_error` の SKIPPED 分岐 / `EVENT_TYPE` ClassVar / `FAILURE_KIND` を削除**。VO 構築失敗(FAILED)は `except Exception` 側で従来どおり残る |
| briefing | [briefing.py:268(task)](../../backend/app/queue/tasks/briefing.py) / [cli.py:132](../../backend/app/insights/briefing/cli.py) → [briefing.py:96](../../backend/app/audit/stages/briefing.py) `append_generation_already_exists` | `briefing_generation_already_exists` | log(既存を確認、無ければ追加) | append 呼び出し削除。**未使用になる `append_generation_already_exists` を削除** |
| trend_discovery | [trend_discovery.py:88/120/137(task)](../../backend/app/queue/tasks/trend_discovery.py) + [cli.py:123/154/170](../../backend/app/insights/trend_discovery/cli.py) | `run_already_exists` / `run_no_target_articles` / `run_conflict` | log(cron/cli 両方)| SKIPPED 呼び出しを削除(helper は FAILED で使うため残す)。**未使用になる SKIPPED outcome enum member を削除**(DB CHECK・set 比較テストに無いこと確認)。**cron と cli を同時に**(片方だけは非対称) |
| backfill | [backfill.py:447/601/757](../../backend/app/queue/tasks/backfill.py) | `run_kill_switch_disabled` | 既存 log `backfill_*_disabled` | `_append_backfill_run_event` の SKIPPED 呼び出し削除。**未使用 enum member 削除** |
| backfill | [backfill.py:461/615/771](../../backend/app/queue/tasks/backfill.py) | `run_held_by_stage_hold` | 既存 log `backfill_*_held` + gauge `vector.backfill.held` | 同上 |
| backfill | [backfill.py:494/650/804](../../backend/app/queue/tasks/backfill.py) | `run_no_targets` | 既存 log `backfill_*_empty` + gauge `vector.backfill.backlog` | 同上 |

completion `incomplete_article_missing` / `not_running` を PR-1 対象に含める根拠(2026-07-07 確定): これらは「今まさに消費している作業対象(incomplete_article)そのものが、別 worker に先に完成/claim されて消えた/状態遷移した」benign な冪等・claim race であり、`scrape_html_body_skipped` log(id+outcome_code)で十分追える。§8.2 で残す分析系 MISSING(存続すべき**親**が消えた整合性兆候)とは判別軸が違う(§8.2 参照)。

### 6.1b PR-1 で保留する SKIPPED(REJECTED 再分類が必要)

- **backfill `daily_budget_exhausted`**([backfill.py:494/632/768](../../backend/app/queue/tasks/backfill.py)): SKIPPED でなく REJECTED として監査に残す再分類が必要なため PR-1.5(§7)へ。PR-1 完了時点で到達可能な SKIPPED append はこの 3 箇所のみになる。

### 6.2 完了条件(Done)

text-grep ではなく**到達可能な append 経路**で判定する(定数・未使用メソッドの false positive を避ける)。

- **`EventType.SKIPPED` を audit に append する到達可能な実行経路が、daily_budget の 3 箇所のみ**になる。§6.1 の対象は semantic API method / helper / ClassVar ごと削除済みで、コード上に到達可能な SKIPPED append(および SKIPPED を返す projection 分岐)が残らない。
- 逃がした各事象に対応する log / span が存在することをテストで固定(可観測性の不変条件を pin)。
- consumer(`pipeline_health` / `source_health`)のクエリ・出力が不変(既存テスト green)。
- `/check` 全 suite green。

### 6.3 検証

- 各 stage の producer-side テスト(`test_*_audit_repository.py` / `test_*_task_dispatch.py`)を「SKIPPED を焼かない / log を出す」へ更新。
- 可能なら task 統合テストで「対象分岐が `event_type='skipped'` 行を生成しない」ことを behavioral に assert(grep の代替となる到達可能経路の証拠)。
- `pipeline_health` / `source_health` のテストが無変更で green(consumer 不変の証拠)。

---

## 7. PR-1.5: backfill `daily_budget_exhausted` を REJECTED に再分類

`daily_budget_exhausted` は当初 counter 新設 + 逃がし予定だったが、これは benign skip でなく **「実対象(`found>0`)を 1 日の予算上限で先送りした run レベルの棄却」**(処理を declined)であり、`no_targets`(`found==0`=先送りなし)と質が違う。よって**逃がさず REJECTED として監査に残す**(2026-07-08 ユーザー確定)。既存の briefing `generation_input_empty`(REJECTED=run レベルで処理を declined)と意味論が一貫する。

1. [backfill.py:494/632/768](../../backend/app/queue/tasks/backfill.py) の `RUN_DAILY_BUDGET_EXHAUSTED` emission の `event_type` を `EventType.SKIPPED` → `EventType.REJECTED` に変更。`outcome_code` / `daily_max` / 既存 log(`backfill_*_daily_budget_exhausted`, found=found)はそのまま維持。
2. **新規 counter は作らない**。予算枯渇のアラートが要る場合は Logfire 側で REJECTED 監査行 / log から出す。
3. `daily_max`(payload フィールド / `append_run_event` param)は REJECTED イベントが引き続き書くため維持。docstring の「skip 制御」→「棄却(予算枯渇)/ 失敗」へ追従。
4. REJECTED 監査行が SQL で追えるのは `run_id` / `daily_max` / `outcome_code` / `stage`。`found`(先送り件数)は payload に無く log 限定(SQL forensic に要るなら payload 追加は別スコープ)。

### Done

- `daily_budget_exhausted` が REJECTED になり、到達可能な `EventType.SKIPPED` の append 経路が **ゼロ**になる(監査モデルが {SUCCEEDED, FAILED, REJECTED} に収束。DB CHECK の `'skipped'` はデータ互換のため残す)。
- task/repository テストで `event_type == REJECTED`(行は `'rejected'`)が pin される。
- consumer(`pipeline_health` / `source_health`)は backfill の REJECTED を読まない(dead-read)ため出力不変。
- `/check` green。

---

## 8. PR-2: REJECTED 系の意味論を stage 横断で整理(**実装済み 2026-07-08**)

「REJECTED を名乗る資格があるか」を stage 横断で揃える。監査削減ではなくイベント意味論の再設計なので、コードを変える前に各コードの分類を合意した(下記)。

### REJECTED の定義(本 PR で確定)

**REJECTED = 工程が対象を処理せず下した「恒久的・または調査価値のある突き返し/打ち切り/欠損/見送り」。per-target の監査行として残す価値がある結末。** benign な冪等 skip(別 worker が先に処理済みで no-op)はこれに当たらず、勝者の SUCCEEDED/REJECTED 行と冗長なので log へ逃がす。

### 8.1 論点(**決着**)

- **分析系 ready-build ALREADY_\* (`REJECTED`)**: curation `already_curated` / `already_rejected_as_noise`、assessment `already_in_scope` / `already_out_of_scope`、embedding `already_embedded`。→ **逃がす確定**(F=冪等 skip)。3 stage 一括で log のみに逃がした。機構: 各 `*ReadyBuildBlockedCode` に `is_idempotent_skip` property(nature を domain が所有)を足し、task の `except *ReadyBuildBlockedError` で `if not exc.code.is_idempotent_skip:` を gate に入れて append+commit を skip。log(`*_content_rejected` / `generate_embedding_rejected`, code 付き)と `set_result("skipped")` は無条件維持。escape は **task-owned** で `append_ready_build_blocked`(generic producer)は本体/docstring とも不変。
- **`content_too_large`(curation, `REJECTED`)**: ドメイン content rejection で `reason`(size)を payload 保持 → **残す確定**。
- **dispatch `source_not_registered` / `source_name_invalid`(`REJECTED`)**: 運用/config 由来の恒久的棄却で per-source forensic(source_id/source_name)を持ち、`source_name_invalid` は VO 構築失敗(判断基準2)→ **残す確定**(Q2、2026-07-08)。逃がさないので新 log `dispatch_source_rejected` は不要。

### 8.2 逃がし対象から明示的に外すもの(keep)

- **`aged_out`**(assessment / embedding の `REJECTED`、curation の delete): 「二度と処理しないと決めた恒久的打ち切り」。判断基準 3/4 に直撃するため**残す**。既存 `vector.backfill.aged_out` counter は commit 成功分のみで IntegrityError 競合分を含まず audit 行と非等価。metric があるから消せる、にはならない。
- **欠損兆候**: `ARTICLE_MISSING`(curation) / `CURATION_MISSING`(assessment) / `ANALYZED_ARTICLE_MISSING`(embedding)。「在るはずの行が消えた」整合性欠損の兆候で、純粋な冪等 dup と性質が違う。品質改善のため**残す**(逃がし対象から外す)。

**MISSING の判別軸(completion は逃がす / 分析系は残す)**: 消えたのが「今まさに消費している作業対象そのもの」(completion の incomplete_article。別 worker が先に完成/claim した benign な結末)なら逃がす。消えたのが「その工程が前提とする**親** entity」(curation にとっての analyzable_article、assessment にとっての curation。通常は存続すべき)なら整合性兆候として残す。この軸で completion `incomplete_article_missing`(§6.1、逃がす)と分析系 MISSING(残す)は矛盾しない。

---

## 9. 判断ポイント(確定済み)

1. **completion の `incomplete_article_missing` / `not_running`(SKIPPED)**: **逃がす(PR-1 対象)で確定**(2026-07-07)。理由=冪等スキップ / 「別 worker が先に処理した」claim race は log で十分。`scrape_html_body_skipped` log が id+outcome_code を残す。分析系 MISSING(親の整合性兆候・残す)との判別軸は §8.2。
2. **逃がし先の counter 要否**: benign race/stale/運用ゲートは log(+既存 gauge)のみ。**新規 counter は原則作らない**(`held` の既存 gauge は流用)。`daily_budget_exhausted` は逃がさず REJECTED で監査に残す(§7)。アラートは Logfire 側で監査行/log から出す。
3. **`daily_budget_exhausted` の分類**: benign skip でなく **REJECTED(予算ゲートによる run レベルの先送り=棄却)で確定**(2026-07-08)。`no_targets`(`found==0`)との判別軸は「先送りされる実対象があるか(`found>0`)」。→ PR-2 の REJECTED 論点から本件は除外。
4. **PR の順序**: PR-1 → PR-1.5 → PR-2。PR-2 は合意優先。
5. **PR-2 の分類(確定・実装済み 2026-07-08)**: 分析系 ready-build ALREADY_\*(5 code)= **逃がす**(F=冪等 skip、勝者行と冗長・dead-read)。content_too_large / aged_out×3 / MISSING×3 / dispatch source_not_registered・source_name_invalid / acquisition・completion の content/conversion rejection = **残す**。機構は domain の `is_idempotent_skip` + task gate、escape は task-owned で repository は generic のまま。DB schema 不変(`EventType.SKIPPED` enum / CHECK `'skipped'` は残置)。

---

## 10. 検証コマンド

Done は text-grep でなく到達可能経路で判定する(§6.2)。grep は補助として使い、残置は既知の保留分(completion ready-build ClassVar / daily_budget)であることを確認する。

```
# 参考: SKIPPED を含む箇所の棚卸し。append 経路と DB CHECK 文字列を区別して読む。
grep -rn "EventType.SKIPPED" backend/app --include='*.py' | grep -v test
#   PR-1 完了後の期待残置: backfill daily_budget の append 呼び出し 3 箇所のみ。
#   PR-1.5 完了後: app 内の SKIPPED append 経路はゼロ(daily_budget は REJECTED へ再分類)。
#     'skipped' は models/pipeline_event.py の DB CHECK 文字列としてのみ残る(データ互換で意図的)。

# 変更 stage の検証(/check スキル)
```
