> **SUPERSEDED (2026-05-25)**: 本 spec の stage1 語彙 (`source_fetch` / `article_collection` /
> `SourceFetch*` / `ingest_source`) は **acquisition** に統一済。正本は
> [`stage1-acquisition-vocabulary-unification.md`](./stage1-acquisition-vocabulary-unification.md)。
>
> 主要語彙 (旧 → 新): `source_fetch` → `acquisition` (stage token / `kind`) /
> `article_collection` → `article_acquisition` (dir) / `SourceFetchError` → `SourceAcquisitionError` /
> `SourceFetchFailureHandler` → `SourceAcquisitionFailureHandler` /
> `SourceFetchAuditRepository` → `SourceAcquisitionAuditRepository` /
> `SourceFetchPayload` → `AcquisitionPayload` / `ingest_source` → `acquire_source` (task) /
> `IngestSourceArg` → `AcquireSourceArg`。`fetch` I/O 基層 (ExternalFetchError / FetchedArticle /
> FetchLog 等) は据え置き。本 spec の当該記述は歴史的経緯として残し、現行仕様としては読まないこと。

# PR2.5-B 本番 deploy runbook

PR #371 (commit `c22e9d6`、main 反映済) を本番に展開する手順。Pattern H 経路を
旧 `discovered_articles` 駆動から新 `article_urls` + `pending_html_articles`
3 表駆動に **コヒーレント cutover** する変更で、dual-write 期間は持たない。

このため deploy 中の **書き込み停止 (queue drain) と中途データの TRUNCATE** が
構造的に必須である点が、通常の rolling deploy と異なる。

## 前提

- main は PR #371 + #372 (test hardening) を含む状態
- alembic head は `r2_articles_disc_nullable` (parent: `r1_pending_html_articles`)
- 本番 DB の現状 head は `o16_add_mdpi` 想定 (PR2.5-A/B より前)
- 本番 worker は `worker-fetch` (metadata + content の supervisord 同居) + scheduler 1

## 不変条件 (deploy 中・後で破ったら abort)

1. **deploy 中に新規 ingestion 書き込みを発生させない**
   旧 task と新 task の混在で `discovered_articles` と `article_urls` の整合性が
   壊れる。queue drain が完了するまで cron schedule を全停止する。

2. **TRUNCATE は r2 migration 後・worker 起動前にのみ実行する**
   TRUNCATE のタイミングが migration 前だと FK 制約 (CASCADE) で `articles` も
   消える。r2 migration が FK ondelete を SET NULL に張り直すまで待つ。

3. **rollback は r2 適用後 + worker 起動前に限り完全復帰可能**
   worker が起動して新規 article INSERT (discovered_article_id=NULL 行) を
   作った瞬間から、r2 downgrade は NOT NULL alter で fail する。
   それ以降の rollback は「列 nullable のまま全 worker 旧 image に戻す」のみ。

## 手順

### Phase 1: pre-deploy 計測 (rollback 判定用 baseline)

```sql
-- 現状 head 確認
SELECT version_num FROM alembic_version;
-- 期待: o16_add_mdpi

-- baseline 件数 (rollback 後の整合性確認に使う)
SELECT
  (SELECT COUNT(*) FROM articles)             AS articles,
  (SELECT COUNT(*) FROM discovered_articles)  AS discovered;
```

### Phase 2: queue drain (新規書き込み停止)

`broker_metadata` の cron schedule を止める。Scheduler container を停止すれば
cron 投入が止まり、in-flight task が消化されたら queue は空になる。

```bash
# 1. scheduler container を停止 (cron 投入を止める)
docker compose -f compose.prod.yml stop scheduler

# 2. in-flight task 消化を待つ (RabbitMQ / Redis broker queue 監視)
#    `dispatch_sources` / `ingest_source` / `dispatch_html_fetch_jobs` /
#    `extract_html_body` / `sweep_expired_leases` が全て 0 になるまで
docker compose -f compose.prod.yml exec rabbitmq rabbitmqctl list_queues name messages
# (broker が Redis なら) docker compose exec redis redis-cli LLEN <queue_name>

# 3. 全 task 消化後、worker container も停止 (混在防止)
docker compose -f compose.prod.yml stop worker-fetch
```

完了条件: 全 broker queue が 0、worker process が `idle` ログを最後に出して
exit、`taskiq` の進行ログが完全に止まる。

### Phase 3: alembic migration (r1 + r2 連続適用)

```bash
# r1 (PR2.5-A) + r2 (PR2.5-B) を一気に head へ
docker compose -f compose.prod.yml run --rm backend alembic upgrade head

# 期待ログ:
#   Running upgrade o16_add_mdpi -> r1_pending_html_articles, ...
#   Running upgrade r1_pending_html_articles -> r2_articles_disc_nullable, ...

# 検証: head が r2 で止まっていること
docker compose -f compose.prod.yml run --rm backend alembic current
# 期待: r2_articles_disc_nullable (head)
```

r1 は既存 articles を全件 backfill する DO ブロック付き。失敗ケース:

- `articles.source_url` に重複があると最初の DO ブロックで `RAISE EXCEPTION`
- backfill 後に NULL が残っていると最後の DO ブロックで `RAISE EXCEPTION`

いずれも tx 内で abort、自動で r1 が rollback されるので DB は破壊されない。

### Phase 4: 旧経路データの TRUNCATE

cutover で新規データは `article_urls` 経由で入る。`discovered_articles` 系列の
旧データは Stage 1〜2 の半端な遷移途中の行を含むため、整合性確保のため
**全削除して 0 から再収集** する (PR2.5-A backfill で `articles` 行は
`article_url_id` を持っているので article 本体は失われない)。

```sql
-- 注意: r2 migration 後 (= FK ondelete=SET NULL 後) でなければ実行禁止
--      r2 migration 前だと CASCADE で articles も巻き込まれる

BEGIN;
TRUNCATE TABLE discovered_articles RESTART IDENTITY CASCADE;
TRUNCATE TABLE pipeline_events RESTART IDENTITY;
COMMIT;

-- 検証: discovered_articles が空、articles は維持されている
SELECT
  (SELECT COUNT(*) FROM articles)              AS articles_after,
  (SELECT COUNT(*) FROM discovered_articles)   AS discovered_after,
  (SELECT COUNT(*) FROM article_urls)          AS article_urls_after,
  (SELECT COUNT(*) FROM pipeline_events)       AS events_after;
-- 期待:
--   articles_after = Phase 1 の articles と同じ
--   discovered_after = 0
--   article_urls_after = articles_after (backfill で全件埋まっている)
--   events_after = 0
```

`pipeline_events` を一緒に TRUNCATE する理由: PR2/PR2.5-B で payload schema が
変わっており、古い payload と新規 payload を SQL 集計で混ぜると統計が壊れる。
新スキーマの観測ベースに切り替える。

### Phase 5: worker 起動 + cron 再開

```bash
# 全 worker と scheduler を順に起動 (依存順: backend → worker → scheduler)
docker compose -f compose.prod.yml up -d backend
docker compose -f compose.prod.yml up -d worker-fetch
docker compose -f compose.prod.yml up -d scheduler

# scheduler ログで cron 登録を確認
docker compose -f compose.prod.yml logs --tail=50 scheduler | grep -iE "schedule|cron"
# 期待: dispatch_sources / dispatch_html_fetch_jobs / sweep_expired_leases /
#      その他既存 cron が全て登録されている
```

### Phase 6: post-deploy 検証 (5〜10 分以内)

```sql
-- 1. ingestion が再開し、新規 pending が積まれていること
SELECT status, COUNT(*) FROM pending_html_articles GROUP BY status;
-- 期待 (1〜2 分後): open > 0、または順次 running → 削除

-- 2. dispatch_html_fetch_jobs が cron で走っていること
SELECT stage, event_type, outcome_code, COUNT(*)
  FROM pipeline_events
 WHERE created_at > NOW() - INTERVAL '5 minutes'
 GROUP BY 1,2,3 ORDER BY 1,2,3;
-- 期待: source_fetch=fetched, content_fetch=fetched (or will_retry/dropped_*)

-- 3. 新規 articles 行が article_url_id を持ち discovered_article_id=NULL であること
SELECT
  COUNT(*) FILTER (WHERE article_url_id IS NOT NULL AND discovered_article_id IS NULL) AS new_path,
  COUNT(*) FILTER (WHERE discovered_article_id IS NOT NULL)                            AS legacy
  FROM articles WHERE created_at > NOW() - INTERVAL '5 minutes';
-- 期待: new_path > 0、legacy = 0
```

```bash
# 4. worker ログに ContentFetchService の Outcome dispatch が出ていること
#    worker-fetch は supervisord で metadata + content process を同居させているため、
#    どちらの process のログも `worker-fetch` container にまとまって出る。
docker compose -f compose.prod.yml logs --tail=200 worker-fetch \
  | grep -iE "ContentFetched|ConflictLost|TerminallyDropped|TransientlyDropped"

# 5. sweep_expired_leases が 1 分間隔で動作していること
docker compose -f compose.prod.yml logs --tail=200 worker-fetch \
  | grep "sweep_expired_leases_completed"
# 期待: 1 分おきに swept_count=N (新規 deploy なら 0 が続くのが正常)
```

## rollback 手順

時点ごとに **適用可能な rollback の幅が変わる** 点が本 deploy 特有。

### Phase 3 の途中で migration 失敗 → 自動 rollback

alembic は migration 単位で tx を張る。`r1` の backfill DO ブロックや `r2` の
alter で失敗すれば、その migration の DDL は全て巻き戻る。手動操作不要、
worker は停止したままなのでサービス影響は queue 滞留のみ。

判断: ログを確認 → 原因が **データ依存** (`source_url` 重複等) なら手当て後に
再 `upgrade head`。原因が **migration バグ** なら本番停止のまま下記 Phase A〜C
の rollback で旧 image 運用に戻す。

### Phase 4 の TRUNCATE までで気付いた → r2 + r1 を順に downgrade

```bash
# Phase 4 完了直後 (worker 未起動): r2 → r1 → o16 の順で完全 rollback 可能
docker compose -f compose.prod.yml run --rm backend alembic downgrade r1_pending_html_articles
docker compose -f compose.prod.yml run --rm backend alembic downgrade o16_add_mdpi

# 旧 image を起動
docker compose -f compose.prod.yml up -d
```

注意: TRUNCATE で消した `discovered_articles` / `pipeline_events` は **戻らない**。
deploy 前に `pg_dump` を取っていれば restore できるが、半端なデータの再注入は
新ロジックで重複 INSERT 衝突を起こすので restore せず空のまま再収集する方が
クリーン。

### Phase 5 以降 (worker 起動後) → 完全 rollback は不可、partial で停止

worker が起動した瞬間に新規 articles 行 (discovered_article_id=NULL) が
生まれ、r2 downgrade の `nullable=False` alter は fail する。

選択肢は 2 つ:

**A. NULL 行を一旦削除して r2 downgrade**

```sql
-- 失う情報: 新経路で取得した article 本体 + 関連 article_extractions /
--          extraction_noises / article_embeddings
DELETE FROM articles WHERE discovered_article_id IS NULL;
```

その後 `alembic downgrade -1` (r2 → r1) → `alembic downgrade -1` (r1 → o16) →
旧 image 起動。article_urls / pending_html_articles テーブルも DROP される。

**B. r2 のまま image だけ旧版に戻す (推奨)**

DB は r2 head を維持し、worker container だけ旧 image (PR #371 merge 前) に
revert。旧 image は新カラム (`article_url_id`) を読まないので無視される
(SELECT * の SQL がカラム数増加を許容する asyncpg の挙動)。

```bash
# image tag を 1 つ前の commit (e.g. 508d843 = PR2.5-A merge) に固定
docker compose -f compose.prod.yml.rollback up -d
```

ただし旧 image は `pending_html_articles` を読まないため、Phase 4 までの
`discovered_articles` TRUNCATE で旧経路の中途データも消えており、
旧経路が動き出してもしばらく ingestion が空回りする (新規にゼロから収集)。

## post-deploy 後 24 時間で監視するもの

- `pipeline_events` の `outcome_code` 分布 (`will_retry` / `dropped_*` 比率が
  PR2 の baseline から大きく外れていないか)
- `pending_html_articles.status='running'` の `leased_until` 過去化頻度
  (sweeper が回っていれば 1 分以内に open に戻る)
- `worker-fetch` の OOM / restart 頻度 (新経路で body が大きい記事を
  含むため、メモリ消費の傾向が変わる可能性。content process の OOM は
  supervisord の Pattern B (autorestart=unexpected + startretries=3) で
  個別 retry、3 回失敗で FATAL → container exit → docker restart loop が visible)

## 参考

| 論点 | 場所 |
|---|---|
| migration r1 (PR2.5-A) | `backend/alembic/versions/r1_pending_html_articles.py` |
| migration r2 (PR2.5-B) | `backend/alembic/versions/r2_articles_disc_nullable.py` |
| FK ondelete=SET NULL の意図 | r2 migration docstring |
| backfill 検証ロジック | r1 upgrade 内の DO ブロック 2 箇所 |
| 設計 SSoT | `specs/pipeline-events-stage2-design.md` |
| PR2.5-C で行う後処理 | `discovered_article_id` 列 / 旧 Repository / 旧 Service の撤去 |
