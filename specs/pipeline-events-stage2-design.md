> **HISTORICAL NOTE (Scope 6)**: このファイルは現行の失敗属性契約より前の
> 設計記録。旧分類 enum / 旧 top-level 監査列への参照は履歴としてのみ読む。
> 現行契約は `pipeline-events-failure-attribute-projection.md` を正本とする。

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

# Stage 2 (content_fetch) 監査統合 + Stage 2 リトライ基盤再設計

PR2 / PR2.5 の設計討議メモ。

ADR: `docs/observability/pipeline-events-design.md`
ロードマップ: memory `project_pipeline_events_pr_roadmap.md`

履歴:
- 2026-05-05 初版 (PR2 audit + PR2.5 列追加 simple 案)
- 2026-05-05 更新 (PR2.5 を 3 テーブル分離 + 3 段リトライ基盤に拡張)
- 2026-05-05 大幅再設計 (discovered_articles 退役 + ReadyForArticle 分岐方針へ転換)
- 2026-05-05 確定 (3 テーブル構成: article_urls / articles / pending_html_articles)
- 2026-05-05 確定 (state model を lease 方式に: status open/running/closed + ready_at + leased_until)

---

## 背景

PR1 で `pipeline_events` 監査基盤 + Stage 1 (`source_fetch`) 統合済。
PR1.5 で Fetcher 型階層整理 + metadata observation 活性化 済。

Stage 2 (= Pattern H 2 段目 = 現 `extract_html_body` task) は監査ゼロだったので
PR2 で audit を入れた (#369 実装済)。その過程で以下が判明:

1. **現状のリトライ設計が機能していない**:
   - `SimpleRetryMiddleware(default_retry_count=0)` + `max_retries=3` は backoff 無し
   - 失敗 → 即時 → 失敗 → 即時 → 失敗 → 即時 → 諦め (数秒で 3 連敗消化)
   - 数秒で復旧する瞬断以外は immediate retry が無価値
   - 永続失敗 (404 等) を抑止する仕組みが無い (RSS feed window に居る限り毎 cron 叩く)
   - exhausted で StagedArticle が破棄され、RSS window から落ちると **データ消失**

2. **データモデルが Pattern H 時代の遺物**:
   - `discovered_articles` は「Pattern H しかなかった時代」の URL 登録テーブル
   - Pattern R 確立後、Pattern R 経路の URL も discovered_articles に積んでおり責務が混在
   - 「URL を見た事実」と「Stage 2 fetch 状態」が同テーブルにあるため
     **永続失敗の理由を sparse カラムとして discovered_articles に持たせる悪臭** が発生
   - `Stage 2 監査基盤を正しく設計するには、まずデータモデルを正常化する必要がある`

→ PR2.5 で **データモデル正常化 + リトライ基盤再設計** をまとめて行う。

---

## 失敗モード分類

| # | 失敗 | 取りたい行動 | 持続時間典型 |
|---|---|---|---|
| 1 | 接続瞬断 / DNS 瞬間失敗 | 即 retry (Tier 1) | 1-10 秒 |
| 2 | HTTP 502/504 (gateway blip) | 即 retry → 失敗なら delay | 10 秒-1 分 |
| 3 | HTTP 503 (service unavail) | delay 付き retry (Tier 2) | 1-30 分 |
| 4 | HTTP 429 (rate limit) | **別軸の問題** (ソース別設計) | 別軸 |
| 5 | timeout 継続 | delay 付き retry | サーバ負荷次第 |
| 6 | HTTP 401/403/404/410/451 / SSRF blocked | 即 drop, 二度と試さない | 永続 |
| 7 | ExtractionEmpty (parse_error / not_html / quality_gate) | 即 drop | 永続 (サイト構造) |
| 8 | promotion Failed (body_too_short / pubdate_missing 等) | 即 drop | 永続 (RSS+HTML 両方の問題) |

---

## PR2: ContentFetchService 切り出し + audit 統合 (実装済 #369)

### 決定 #1: Outcome shape

```python
@dataclass(frozen=True, slots=True)
class ContentFetched:
    article: Article  # race-lost 時の既存検出も含む

@dataclass(frozen=True, slots=True)
class TerminallyDropped:
    reason_code: str

@dataclass(frozen=True, slots=True)
class TransientlyDropped:
    reason_code: str

ContentFetchOutcome = ContentFetched | TerminallyDropped | TransientlyDropped
```

### 決定 #2: エラーハンドリング

- `TemporaryFetchError` は素通しで raise → task が retry 判断
- `PermanentFetchError` / `ExtractionEmpty` / promotion `Failed` / race 完全敗北 → audit + `TerminallyDropped`
- 成功 / race-lost で既存検出 → audit + `ContentFetched`
- last-attempt 時のみ `svc.audit_exhausted(staged, attempt, exc)` を task が呼ぶ

### 決定 #3: ContentFetchPayload

`backend/app/observability/domain/payloads.py` の skeleton に
`reason_code` / `body_length` を追加。outcome_code は 3 値:
- `"fetched"` (SUCCEEDED)
- `"dropped_terminal"` (SKIPPED)
- `"dropped_transient"` (FAILED)

### PR2 ステータス

PR #369 (commit a43da74) で merge 済。
ただし PR2.5 のデータモデル変更後、本 PR の audit 入力対象 (StagedArticle / discovered_article_id payload) は変わるため、PR2.5 内で追従改修が必要。

---

## PR2.5: データモデル正常化 + リトライ基盤再設計

### 設計原則

#### 原則 1: Fetcher が「次段階の品質を担保する」責務を持つ

各 Fetcher は entry を yield する際、その entry が次段階に進める品質を持つかどうかを判断する。
判断ロジックは Fetcher にコピペするのではなく、`ReadyForArticle.try_advance_from` を **分岐器** として再利用する。

- 戻り値 `ReadyForArticle` → RSS で前提充足、Pattern R として直行
- 戻り値 `Failed(...)` → RSS では充足できない、HTML 抽出での救済対象
- 戻り値 `None` → 既存 article / noise 等の確定スキップ

これにより:
- 「Pattern R / R+H / H」というソース分類が **コードから消える** (research / memory には残るが実装上の概念ではない)
- 「揃ったか」の判定 SSoT は `ReadyForArticle.try_advance_from` の 1 箇所
- entry 単位で動的に R / H が分かれる (同一ソース内で両経路が混在しうる)

#### 原則 2: URL の一意性は専用 identity 台帳が担う

3 テーブル構成で責務を分離する:

- `article_urls` = URL の identity 台帳 (一意性 SSoT、不変)
- `analyzable_articles` = 完成結果 (Pattern R 直行 / Pattern H 救済後の昇格、両方ここに着地)
- `pending_html_articles` = HTML 取得待ちの作業領域 (Pattern H 専用)

analyzable_articles と pending_html_articles はそれぞれ `article_urls.id` を UNIQUE FK として参照。
**DB の UNIQUE 制約だけで cross-table dedup が物理的に保証される** (advisory lock 等のランタイム規律に頼らない)。

`discovered_articles` は退役する。識別子としての URL 一意性は `article_urls` が、
「URL を見た事実」の audit は `pipeline_events` の時系列が担う。

#### 原則 3: 「理由」は監査、「状態」は state table

永続失敗の reason を `pending_html_articles` の row column として持たせない (sparse 問題の回避)。
`pipeline_events.payload.reason_code` で時系列に記録し、state table はそのときの状態 (open / closed / completed-then-deleted) のみ表現する。

#### 原則 4: RSS metadata は pending のみに置く

`article_urls` は pure identity (URL + 初出 source/timestamp のみ)。
RSS で取れた title / published_at / partial body 等の **Stage 2 で HTML とマージするための情報** は `pending_html_articles` に置く。

理由:
- Pattern R は articles に直接書くので RSS metadata を中継する必要が無い → article_urls に持たせると sparse 列になる
- Pattern H が必要とする metadata は pending の自分の行で完結 (read 一回)
- article_urls を identity に純化することで、責務混在の悪臭を防ぐ

---

### データフロー (新設計)

```
[Stage 1 cron: ingest_source]
  Fetcher が各 entry を yield
    │
    ├── 1. noise check (URL pattern / title pattern)
    │     noise 判定 → 何もしない (article_urls 行も作らない、再 discover で再評価可能)
    │
    ├── 2. INSERT INTO article_urls (normalized_url, ...) ON CONFLICT DO NOTHING RETURNING id
    │     id が返らなかった (既知 URL) → スキップ
    │     id が返った (新規 URL) → 続行
    │
    └── 3. ReadyForArticle.try_advance_from で振り分け
          ├── ReadyForArticle 成立 (RSS で充足)
          │     → articles INSERT (article_url_id = 新規 candidate id)
          │     → 同 tx で commit (Pattern R 完結)
          │
          ├── Failed (品質不足、典型: body_too_short)
          │     → pending_html_articles INSERT
          │       (article_url_id = 新規 candidate id, status='open',
          │        ready_at=NOW, leased_until=NULL, staged_attributes={...})
          │     → 同 tx で commit
          │     → commit 後に extract_html_body.kiq(pending_id)
          │       (kiq は加速通知、SSoT は DB の pending 行)
          │
          └── None (既存 article 等の確定スキップ — try_advance_from 内部判定)
                → article_urls 行は INSERT 済だが articles も pending も作らない
                  (このケースが発生するのは並行 race のみ、稀)

[Stage 2 cron: dispatch_html_fetch_jobs] (1 分間隔)
  UPDATE pending_html_articles
    SET status='running', leased_until = NOW() + lease_duration, attempt_count = attempt_count + 1
    WHERE id IN (
      SELECT id FROM pending_html_articles
      WHERE status='open' AND ready_at <= NOW()
      ORDER BY ready_at FOR UPDATE SKIP LOCKED LIMIT N
    )
    RETURNING id
  → 取れた id ごとに extract_html_body.kiq(pending_id)

[extract_html_body task]
  ContentFetchService.execute(pending_id) → DB から pending_html_articles + article_urls を load
    │
    ├── 成功 (HTML 取得 + ReadyForArticle 成立)
    │     → articles INSERT (article_url_id = candidate id, staged_attributes と HTML body をマージ)
    │     + pending_html_articles 行を DELETE (同 tx)
    │
    ├── Permanent / ExtractionEmpty / promotion Failed
    │     → status='closed', leased_until=NULL
    │       (理由は pipeline_events.payload.reason_code で監査参照、state には残さない)
    │
    └── Temporary (TemporaryFetchError 系)
          → status='open', ready_at = エラー種別に応じた次回時刻, leased_until=NULL
            (再投入は cron poller が次 tick で拾う、Service は kiq しない)
            上限到達なら status='closed' + reason_code='temporary_exhausted'

[sweeper cron: sweep_expired_leases] (1 分間隔)
  UPDATE pending_html_articles
    SET status='open', ready_at=NOW(), leased_until=NULL
    WHERE status='running' AND leased_until <= NOW()
  → 死んだ worker が握ったまま lease 切れの行を救出
```

### Pattern R / Pattern H の経路

| 経路 | article_urls | articles | pending_html_articles |
|---|---|---|---|
| RSS で完結 (Pattern R) | あり | あり | **なし** (経由しない) |
| HTML 抽出待ち (Pattern H 初回) | あり | なし | status=open, ready_at=NOW, leased_until=NULL |
| HTML 抽出中 (in-flight) | あり | なし | status=running, leased_until=NOW+lease |
| HTML 抽出後待機 (再試行 backoff) | あり | なし | status=open, ready_at=future, leased_until=NULL |
| HTML 抽出永続失敗 | あり | なし | status=closed, leased_until=NULL |
| HTML 抽出成功 | あり | あり | **なし** (削除済) |

article_urls は全経路で永続。articles と pending_html_articles の有無で経路と状態が一意に決まる。

### Retry policy (エラー種別ごとの delay schedule)

エラー種別によって復旧時間と粘り方が異なるため、uniform exponential backoff ではなく
**per-error policy** で扱う。

| エラー | delay schedule (分) | 試行 | 諦めまで | 性質 |
|---|---|---|---|---|
| ConnectionError / DNS fail | **0.5 → 1 → 2 → 5 × 5** | 8 回 | ~28.5 分 | blip-class、max 単発 5 分 |
| HTTP 502/504 (gateway blip) | **0.5 → 1 → 2 → 5 × 5** | 8 回 | ~28.5 分 | blip-class、max 単発 5 分 |
| HTTP 503 (no Retry-After) | **5 → 15 → 30 → 60 × 9** | 12 回 | ~10 時間 | outage-class、長期粘り |
| HTTP 503 with `Retry-After` | header 値、後続 cap 60 | 12 回 | header + cap | サーバ指示尊重 |
| HTTP 429 (rate limit) | PR2.5 範囲外、仮 60 × 1 | 1 回 | — | 別軸 (per-source rate limit) |
| Read timeout | **2 → 5 × 7** | 8 回 | ~37 分 | blip-class 寄り、max 単発 5 分 |
| Unknown TemporaryFetchError | 5 → 15 → 30 → 60 × 3 | 6 回 | ~3 時間 | outage 寄り保守的 |

設計原則:
- **blip-class** (Connection/502/504): 真の瞬断は秒〜1 分で復旧、密に retry → 5 分以上は構造的問題なので max 単発 5 分で打ち切らずに同じ間隔で粘る
- **outage-class** (503): 数十分〜数時間の outage を想定、長尺 tail
- **server-instructed** (Retry-After): サーバ指示を Vector policy より優先
- **「諦め」の意味**: `pending.status='closed'` + `pipeline_events` に `reason_code='temporary_exhausted'` 焼付。自動再試行なし、ops が SQL で reopen 可能

### Dispatch 機構: cron poller のみ

全 delay range で **cron poller (1 分間隔) のみ** で再投入する。broker schedule_by_time は採用しない。

理由:
- **Service が kiq しない原則** との整合 (kiq は dispatcher = cron / task の責務、Service は DB 状態更新のみ)
- DB の `pending_html_articles.ready_at` を SSoT として一元化、Redis stream の message lifecycle を考慮する必要がなくなる
- Stage 2 は外部 HTTP 取得で sub-1-min latency を要求しない領域、cron 1 分間隔で実用上問題なし
- per-error policy の最短 delay = 0.5 分。cron 1 分粒度を加えても実効 0.5〜1.5 分、blip-class の典型復旧時間 (秒〜1 分) と同オーダで影響なし
- テスト容易性 (broker mock 不要、cron + DB の挙動だけで全 retry path を再現可能)

retry 時の Service ロジック (kiq 一切なし、DB のみ更新):

```python
async def _reschedule(self, pending_id: int, delay_minutes: float, exc: Exception) -> None:
    next_at = utcnow() + timedelta(minutes=delay_minutes)
    # 失敗した task の lease を解放しつつ次回時刻を設定
    await update_pending(
        pending_id,
        status="open",
        ready_at=next_at,
        leased_until=None,
    )
    # audit に will_retry 行を焼く (FAILED, outcome="will_retry")
    await self._audit_will_retry(pending_id, next_at=next_at, exc=exc)
    # broker schedule_by_time は呼ばない — cron poller が次の tick で拾う
```

### Race-safety

picking は lease 取得 UPDATE で実装する:

```sql
UPDATE pending_html_articles
SET status = 'running',
    leased_until = NOW() + INTERVAL '5 minutes',
    attempt_count = attempt_count + 1,
    updated_at = NOW()
WHERE id IN (
  SELECT id FROM pending_html_articles
  WHERE status = 'open'
    AND ready_at <= NOW()
  ORDER BY ready_at
  LIMIT 100
  FOR UPDATE SKIP LOCKED
)
RETURNING id, article_url_id, source_id, staged_attributes, attempt_count;
```

`SELECT FOR UPDATE SKIP LOCKED` で multi-worker 安全。`status='running'` 遷移で他 tick から見えなくなる。task が落ちて lease が切れた場合は sweeper が再 open。

CHECK 制約で state 整合性を構造的に強制する:

```sql
CHECK (
  (status = 'open'    AND leased_until IS NULL) OR
  (status = 'running' AND leased_until IS NOT NULL) OR
  (status = 'closed'  AND leased_until IS NULL)
)
```

「open なのに lease が残ってる」「running なのに lease が NULL」のような不整合 state を DB レベルで防ぐ。

### lease_duration の指針

```
lease_duration = task_timeout × 3〜5
```

- 現在 `extract_html_body.timeout = 60 秒` → `lease_duration = 5 分` (= 5×)
- task timeout を変更する際は lease_duration も比例で調整する
- worker は正常なら 60 秒以内に終わる、5 分はネットワーク遅延 / GC pause / DB lock 待ち等のマージン
- 5 分を超えて lease 保持されている = 確実に worker 異常終了

### success 時は pending を削除する (article_urls は残す)

| 行のライフサイクル | article_urls | pending_html_articles |
|---|---|---|
| Stage 1 で Failed → INSERT | あり (新規) | status='open', ready_at=NOW, leased_until=NULL |
| Stage 2 in-flight | あり | status='running', leased_until=NOW+5min |
| Stage 2 待機中 (再試行 backoff) | あり | status='open', ready_at=future, leased_until=NULL |
| Stage 2 永続失敗 | あり | status='closed', leased_until=NULL (残す) |
| Stage 2 成功 | あり (永続) | **削除** (articles INSERT と同 tx) |

設計の根拠:
- **article_urls は永続** = URL 一意性 SSoT。article 手動削除しても URL は再 discover で重複扱い (意図的な caching)
- **pending は成功時に削除** = pending = 「作業中」が 1 責務、終了したら退場
- **closed pending は残す** = 「もう試さない」マーカー。article_urls だけでは "永続失敗" と "成功済" の区別がつかないので pending.closed が情報を持つ
- **「成功した URL の attempt 履歴」は `pipeline_events` で時系列に追える** = state table に詰め込まない

### スキーマ案

```sql
-- 新設 (URL identity 台帳)
CREATE TABLE article_urls (
  id                     BIGSERIAL PRIMARY KEY,
  normalized_url         TEXT NOT NULL UNIQUE,
  original_url           TEXT NOT NULL,
  first_seen_source_id   BIGINT NOT NULL REFERENCES news_sources(id),
  first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 既存 articles の改修 (FK 化)
ALTER TABLE articles
  ADD COLUMN article_url_id BIGINT NULL UNIQUE REFERENCES article_urls(id) ON DELETE RESTRICT;
-- データ移行後 NOT NULL 化
-- (既存 analyzable_articles.source_url は移行期間中は併存、最終的に DROP)

-- 新設 (HTML 取得待ちの作業領域)
CREATE TABLE pending_html_articles (
  id                     BIGSERIAL PRIMARY KEY,
  article_url_id         BIGINT NOT NULL UNIQUE
                         REFERENCES article_urls(id) ON DELETE CASCADE,
  source_id              BIGINT NOT NULL REFERENCES news_sources(id),
  status                 TEXT NOT NULL CHECK (status IN ('open','running','closed')),

  -- 完成材料 (Stage 1 で取れた、Stage 2 で HTML body と merge する情報)
  -- ソース種別 (RSS / sitemap-only / HTML listing / API) で取れる field が異なるため
  -- JSONB + Pydantic Optional で表現
  staged_attributes      JSONB NOT NULL,

  -- 「次に何をするか」の状態 (lease 方式)
  ready_at            TIMESTAMPTZ NULL,             -- いつから picking 可能か (open のみ意味を持つ)
  leased_until           TIMESTAMPTZ NULL,             -- worker への lease 有効期限 (running のみ意味を持つ)
  attempt_count          INTEGER NOT NULL DEFAULT 0,

  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- state model 整合性: 「open なのに lease が残る」「running なのに lease NULL」を防ぐ
  CONSTRAINT pending_html_articles_state_consistency CHECK (
    (status = 'open'    AND leased_until IS NULL) OR
    (status = 'running' AND leased_until IS NOT NULL) OR
    (status = 'closed'  AND leased_until IS NULL)
  )
);

-- picking 用 (open かつ ready_at が現在時刻以下)
CREATE INDEX ix_pending_html_articles_ready
  ON pending_html_articles (ready_at)
  WHERE status = 'open';

-- sweeper 用 (running かつ lease が切れたもの)
CREATE INDEX ix_pending_html_articles_expired_lease
  ON pending_html_articles (leased_until)
  WHERE status = 'running';

-- 退役 (PR2.5 終盤で完全削除)
-- DROP TABLE discovered_articles;
```

設計上の保証:
- `article_urls.normalized_url` UNIQUE で URL の一意性が **DB 物理的に** 担保される
- `analyzable_articles.article_url_id` UNIQUE + `pending_html_articles.article_url_id` UNIQUE により、**1 candidate に対し analyzable_articles/pending それぞれ最大 1 行**
- 「URL が articles と pending の両方に存在する」状態は schema 的に発生不能 (それぞれが article_urls.id を UNIQUE 参照、Stage 1 の INSERT 順序で常に articles or pending のどちらか一方しか作られない)
- advisory lock 等のランタイム規律は不要

### staged_attributes の Pydantic スキーマ

```python
class StagedArticleAttributes(BaseModel):
    """Stage 1 で取れた、Stage 2 で HTML 本文と merge して articles を完成させる材料。
    ソース種別 (RSS / sitemap-only / HTML listing / API) によって取れる field が
    異なるため、すべて optional。
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = None              # RSS にはあり / sitemap-only では None
    published_at: datetime | None = None  # RSS pubDate / sitemap lastmod / その他
    # 将来追加: author / summary / categories / language ...
```

スキーマ進化の運用ルール (後方互換のみ):

| 変更 | 可否 | 方法 |
|---|---|---|
| field 追加 | OK | `optional + default = None` で追加 |
| field 削除 | 段階的に OK | `deprecated` マーク → 1-2 リリース後に削除 (老朽 closed 行が drain するのを待つ) |
| field rename | 段階的に OK | Pydantic alias で旧名を受ける期間を設ける、または `jsonb_set` で migration |
| 型変更 (互換性なし) | NG | 別 field を追加して移行 |

### 監査参照キー

`pipeline_events.payload.article_url_id` を Stage 2 events の SSoT 参照キーとする。
理由: article_urls は永続なので、pending が削除/closed のどの状態でも参照が resolve する。

```sql
-- URL X に何が起きたかを時系列で追う典型 query
SELECT pe.*
FROM pipeline_events pe
JOIN article_urls au ON au.id = (pe.payload->>'article_url_id')::bigint
WHERE au.normalized_url = '...'
ORDER BY pe.occurred_at;
```

### URL 正規化

`normalized_url` の正規化ルール (最低ライン):

1. lowercase host
2. tracking parameters strip (`utm_*` / `fbclid` / `gclid` / `mc_*` / `_hsenc` / `_hsmi` 等)
3. trailing slash 正規化 (path 末尾の `/` を統一、ただし root path は除外)
4. fragment (`#...`) 除去
5. scheme は保存 (http と https は別 URL として扱う、サイトリダイレクト判定は別軸)

`original_url` は正規化前を保存。表示時は original を使い、dedup は normalized で行う。

---

## Stage 1 audit (`SourceFetchPayload`) の semantic 再定義 (κ)

PR1 で実装済の `SourceFetchPayload` は旧データモデル前提の semantic を持つ。新設計 (Fetcher の戻り値が `Ready / Failed / None`、`Failed` は失敗ではなく Pattern H への振り分けを意味する) で同じ field 名のまま使うと意味が反転するため、PR2.5-B で再定義する。

### 命名方針: 経路非依存

`html_fetch_*` 系の HTML 固定命名は将来 (API detail fetch / PDF fetch 等) で再 rename を強いる。代わりに **「補完を待つ」という意図を表現する `completion_*` 命名** を採用する。

| 旧 field | 新 field | 意図 |
|---|---|---|
| `persisted_count` | `article_created_count` | ReadyForArticle 成立で articles 直 INSERT した数 (= "create" の意図が明確) |
| `staged_count` | `completion_queued_count` | 後段補完用に pending 等に積んだ数 (HTML 以外も含む経路非依存) |
| `failed_count` (Failed = 落ち) | `failed_count` (真の失敗のみ) | semantic を「Fetcher 内例外 / DB INSERT 失敗」に絞る |
| `failed_codes` (Failed.reason.code) | `completion_reason_codes` | 旧「Failed」は新設計だと "完成待ち振り分け理由"、命名を semantic に合わせる |
| (なし) | `entry_count` | Fetcher が yield した総数 (invariant 検証用) |
| (なし) | `skipped_codes` | known_url / noise_matched / existing_article 等の内訳 |
| (なし) | `failed_codes` | 例外 class FQN ベースの真の失敗内訳 |

### count fields は `int = 0` で常時 populate

`int | None = None` ではなく `int = 0` を採用する。

理由:
- **invariant が常時検証可能**: `entry_count == article_created_count + completion_queued_count + skipped_count + failed_count` が成立すべき
- **集計クエリで COALESCE 不要**: `SUM(entry_count)` が単純に書ける
- **`None` の意味が明確化**: 通常 audit では絶対出現しない、出現したら writer のバグ

dict fields (`completion_reason_codes` / `skipped_codes` / `failed_codes`) は **sparse 維持** (`dict[str, int] | None = None`)。typical な fetch では空 dict と None の区別に集計価値がなく、row size を抑えるため。

### 確定 shape

```python
class SourceFetchPayload(BasePipelineEventPayload):
    """Stage 1 — 1 ソース 1 fetch の集約サマリ。

    Fetcher が yield した entry を「即記事化」「後段補完待ち」「スキップ」「失敗」
    に分類した件数集計を記録する。後段補完経路は将来 HTML 以外
    (API detail fetch / PDF fetch 等) も追加されうるが、本 payload は経路非依存
    の集計のみを持つ (経路別内訳は将来必要になったら追加)。

    Invariant:
      entry_count == article_created_count + completion_queued_count
                     + skipped_count + failed_count
    """

    kind: Literal["source_fetch"] = "source_fetch"
    fetcher_class: str | None = None

    # 件数集計 (常に populate、invariant 成立)
    entry_count: int = 0
    article_created_count: int = 0
    completion_queued_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0

    # 内訳コード (sparse、必要時のみ)
    completion_reason_codes: dict[str, int] | None = None
    skipped_codes: dict[str, int] | None = None
    failed_codes: dict[str, int] | None = None

    # metadata observation (PR1.5 で活性化、維持)
    metadata_fields_observed: list[str] | None = None
    metadata_sample: dict[str, Any] | None = None

    # S 級 snapshot (失敗時のみ、継承)
    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None
```

### kind versioning は不要

- 本番未デプロイ状態 = 旧 semantic の audit データはローカルに少量のみ
- discriminator `kind="source_fetch"` のまま evolve、`source_fetch_v2` への分岐はしない
- pydantic 型変更で旧データの読込が validation error になる問題は **TRUNCATE で clean slate** にして回避

### TRUNCATE 対象

PR2.5-B cutover 時、以下を TRUNCATE する:
- `discovered_articles` (旧 Pattern H 経路の作業領域、新設計で退役)
- `pipeline_events` (旧 semantic の Stage 1 audit を破棄、新 semantic で記録再開)

→ PR2.5-B の deploy 手順に明記する。

### 不採用とした設計 (YAGNI)

- `completion_route_counts: dict[str, int]` (経路別内訳、`{"html": 12, "api": 3}` 等)
  - 現時点で route は HTML のみ、追加すると常に `{"html": N}` の単一 key dict が並び `completion_queued_count` と完全重複
  - **API 経路を実装する PR と同時に追加** する方が history 連続性も保てる

---

## PR2 audit の追従改修 (PR2.5-B 内で実施)

PR2 (#369) は旧データモデル (`StagedArticle` + `discovered_articles`) 前提で実装済。
PR2.5-B の cutover 時に ContentFetchService と extract_html_body task を新モデル駆動に書き換える。

### task の引数 (ι.1)

`extract_html_body.kiq(pending_id: int)` のみ。snapshot は乗せない。

理由:
- spec の「kiq は加速通知、SSoT は DB の pending 行」と一貫
- kiq message が軽い (~20B)
- at-least-once 重複配送 / lease 衝突時、worker は「SELECT 空 → 静かに exit」で扱える (snapshot 方式だと UNIQUE 違反 IntegrityError で騒音)
- retry 時 (schedule_retry) は DB から最新 (attempt_count 等) を読み直す必要があるため、初回 dispatch だけ snapshot にしても不一貫

worker 起動時の SELECT は 1 クエリで済む:

```sql
SELECT p.id, p.article_url_id, p.source_id, p.staged_attributes, p.attempt_count,
       au.normalized_url, au.original_url,
       ns.name AS source_name
FROM pending_html_articles p
JOIN article_urls au ON au.id = p.article_url_id
JOIN news_sources  ns ON ns.id = p.source_id
WHERE p.id = $1
```

worker が message を受けた時点で `status='running'` になっているはず (cron poller が lease 取得済)。
重複配送等で `status` が `running` でなければ静かに exit する (Service 内部で判定):

```python
async def execute(self, pending_id: int) -> ContentFetchOutcome | None:
    pending = await self._load(pending_id)
    if pending is None:
        # 既に成功して DELETE 済 (at-least-once 重複配送)
        return None
    if pending.status != 'running':
        # lease 衝突 or 別 worker が処理中、自分は退場
        return None
    ...
```

### 機械的に確定する変更 (ι.A / ι.B / ι.C)

- `ContentFetchPayload.discovered_article_id` → `article_url_id`
- `analyzable_articles.discovered_article_id` → `analyzable_articles.article_url_id` (FK 切替)
- `ArticleRepository.find_by_discovered_article_id` → `find_by_article_url_id`

これらは検索置換に近い。

### `attempt` の SSoT (ι.2)

`pending_html_articles.attempt_count` が SSoT。taskiq の `ctx.message.labels.get("retry_count", 0)` は使わない。

理由:
- ι.4 方針で taskiq retry 機構を捨てるため、taskiq の `retry_count` は常に 0 になり意味を持たない
- cron poller (1 分間隔) のみで retry を回す設計と整合 (再投入の SSoT は DB の ready_at)
- lease 取得 UPDATE で `attempt_count = attempt_count + 1` と原子的に増えるので race フリー

semantic:
- 初期値 `0`、最初の lease 取得で `1` になる (= その execution の試行番号)
- 失敗時の `schedule_retry` は `ready_at` のみ更新、`attempt_count` は触らない (次の lease 取得で +1)
- `pipeline_events.attempt` カラムには Service が SELECT した attempt_count をそのまま書く
- policy 判定 (max_attempts 到達) は worker (Service) が DB の attempt_count を見て決める。poller を policy に依存させない (シンプル)

### 競合発生時の挙動 (ι.3)

**設計意図**: 競合は非同期処理である以上、論理的には常に起きうる。新設計は「競合を消す」のではなく **「競合の発生位置を articles INSERT から pending job claim に移す」** ことで、競合粒度を制御している。

防御層:

| 層 | 機構 | 競合の捌き方 |
|---|---|---|
| 1 | cron poller の `UPDATE...RETURNING + FOR UPDATE SKIP LOCKED` | pending claim 段階で skip |
| 2 | message に `pending_id` のみ + worker 開始時の `status='running'` 確認 | claim 整合性違反は静かに exit |
| 3 | `analyzable_articles.article_url_id UNIQUE` + `pending_html_articles.article_url_id UNIQUE` | last-resort: IntegrityError として観測 |

これは悲観的 claim + lease + idempotency constraint であり、楽観的ロックではない。

#### Outcome variant 拡張

```python
@dataclass(frozen=True, slots=True)
class ContentFetched:
    """winner — articles INSERT 成功、extract_content.kiq に chain"""
    article: Article

@dataclass(frozen=True, slots=True)
class ConflictLost:
    """別 worker が articles を先に作った。chain は走らせない (winner が chain 済)。
    URL の処理自体は成功扱い (system 的には fetched と同等)。"""
    pass

@dataclass(frozen=True, slots=True)
class TerminallyDropped:
    """URL 自体が dead、二度試しても無意味。"""
    reason_code: str

@dataclass(frozen=True, slots=True)
class TransientlyDropped:
    """retry budget を使い切った。次 cron で再試行する価値あり。"""
    reason_code: str

ContentFetchOutcome = ContentFetched | ConflictLost | TerminallyDropped | TransientlyDropped
```

#### 永続化ロジック

```python
try:
    persisted = await article_repo.save(...)
except IntegrityError:
    await session.rollback()
    existing = await article_repo.find_by_article_url_id(article_url_id)
    if existing is not None:
        # 競合に負けた、勝者が article を作って chain も発火済
        await self._audit_conflict_lost(...)
        return ConflictLost()
    else:
        # UNIQUE 違反だが行が見つからない = transaction visibility / isolation 異常
        await self._audit_terminal(reason_code="article_persist_anomaly", ...)
        return TerminallyDropped("article_persist_anomaly")
```

#### audit outcome_code 一覧 (Stage 2)

| outcome_code | event_type | 意味 |
|---|---|---|
| `fetched` | SUCCEEDED | winner、article INSERT 成功 |
| `conflict_lost` | **SKIPPED** | loser、winner が処理済 (重複 audit を SUCCEEDED で並べないため SKIPPED) |
| `dropped_terminal` | SKIPPED | URL 自体が dead (永続失敗) |
| `dropped_transient` | FAILED | retry budget 切れ |

`conflict_lost` を SKIPPED にする理由: 同じ URL に対する `fetched` (winner) は 1 件のみ存在すべき。loser を SUCCEEDED にすると「複数回 fetch されたか?」という audit ノイズになる。SKIPPED は「この worker は何もしなかった」を意味的に表現。

#### task 側の dispatch

```python
match outcome:
    case ContentFetched(article=article):
        # ReadyForExtraction 経由で extract_content.kiq
    case ConflictLost() | TerminallyDropped() | TransientlyDropped():
        # chain しない
```

### retry/exhausted 判定の責務 + dispatch 機構 (ι.4)

**設計原則**: 責務を 4 つの層に明確に分離する。

| 層 | 責務 |
|---|---|
| poller (cron) | DB を見て ready な pending job を claim し、taskiq に投入 |
| task (extract_html_body) | `pending_id` を受けて Service を呼ぶ。Outcome を見て成功時のみ `extract_content.kiq` で chain |
| Service (ContentFetchService) | 1 件の HTML fetch を実行、結果に応じて DB 状態 (status / ready_at / leased_until / attempt_count) を更新 + audit を焼く |
| retry_policy | エラー種別 + attempt_count から次の ready_at / exhausted を決める純関数 module |

**Service は kiq 一切しない**:
- extract_content.kiq (chain) は task の責務
- 自己再投入 (broker schedule_by_time) は採用しない (cron poller のみ)
- これにより「業務処理 (Service) と キュー投入 (task / poller)」が分離される

#### task の構造

```python
@broker_content.task(
    task_name="extract_html_body",
    timeout=60,
    max_retries=0,           # taskiq retry 機構は完全に殺す
    retry_on_error=False,
)
async def extract_html_body(pending_id: int, ctx: Context = TaskiqDepends()) -> None:
    svc = ContentFetchService(ctx.state.session_factory)
    outcome = await svc.execute(pending_id)

    match outcome:
        case ContentFetched(article=article):
            ready = await ReadyForExtraction.try_advance_from(...)
            if ready is not None:
                await extract_content.kiq(ready)
        case ConflictLost() | TerminallyDropped() | TransientlyDropped():
            return None
```

#### Service の構造

```python
class ContentFetchService:
    async def execute(self, pending_id: int) -> ContentFetchOutcome:
        pending = await self._load(pending_id)
        if pending is None or pending.status != 'running':
            return ConflictLost()  # 重複配送 / lease 衝突 / 既に成功済

        try:
            html_result = await extractor.fetch(...)
            # promotion / 永続化 (try/except IntegrityError は ι.3 通り)
        except TemporaryFetchError as e:
            return await self._handle_temporary(pending, exc=e)
        except PermanentFetchError as e:
            return await self._handle_terminal(pending, reason="permanent_fetch_error", exc=e)
        # 他の terminal ケース...
        return ContentFetched(article=...)

    async def _handle_temporary(self, pending, *, exc) -> TransientlyDropped:
        policy = retry_policy_for(exc)
        if pending.attempt_count >= policy.max_attempts:
            await self._mark_exhausted(pending, exc=exc)
            return TransientlyDropped(reason_code="temporary_exhausted")
        next_at = utcnow() + policy.next_delay(pending.attempt_count)
        await self._reschedule(pending, ready_at=next_at, exc=exc)
        return TransientlyDropped(reason_code=f"temporary_will_retry_{policy.code}")
```

#### retry_policy module (`app/collection/extraction/retry_policy.py`)

per-error policy table (η の決定内容) を純関数として分離:

```python
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    code: str               # "blip" / "outage" / "retry_after" 等
    max_attempts: int
    delay_schedule: list[float]  # 分単位

    def next_delay(self, attempt_count: int) -> float:
        idx = min(attempt_count, len(self.delay_schedule) - 1)
        return self.delay_schedule[idx]

def retry_policy_for(exc: Exception) -> RetryPolicy:
    # 502/504/Connection → BLIP_POLICY
    # 503 (no Retry-After) → OUTAGE_POLICY
    # 503 with Retry-After → RetryAfterPolicy(header value)
    # ...
```

これにより policy 調整は Service 本体を触らずに済む。

---

## 持ち越し論点 (実装着手前に詰める)

| # | 論点 | 推奨 | 状態 |
|---|---|---|---|
| α | success 時の lifecycle | pending 行削除、article_urls 行残す | **決定** |
| β | cross-table dedup at Stage 1 | article_urls の UNIQUE が SSoT、3 テーブル構成で物理的に担保 | **決定** |
| γ | pending_html_articles のスキーマ詳細 | staged_attributes JSONB + Pydantic Optional、reason 系列は監査参照 | **決定** |
| δ | discovered_articles の退役方法 | dual-write なし、queue drain + TRUNCATE → cutover、4 PR 構成 | **決定** |
| ε | poller の interval | **1 分** (analysis cron と cadence 整合) | **決定** |
| ζ | Tier 1 (in-process) retry | **撤去**、cron poller (1 分間隔) のみで統一 | **決定** |
| η | retry policy | **per-error policy**: blip-class は max 単発 5 分 / outage-class は長尺粘り | **決定** |
| θ | state model + sweeper | **lease 方式**: status open/running/closed + ready_at + leased_until。lease_duration = task_timeout × 3〜5 (= 5 分)。sweeper は `status='running' AND leased_until <= NOW()` を再 open | **決定** |
| ι.1 | task の引数 | **`pending_id: int` のみ** — kiq message 軽量、SSoT は DB の pending 行という設計と一貫、at-least-once / lease 衝突を SELECT 空で静かに exit できる | **決定** |
| ι.2 | `attempt` の SSoT | **DB の `attempt_count` のみ** — lease 取得 UPDATE で +1、taskiq context (`retry_count`) は使わない、policy 判定も Service が DB を見て決める | **決定** |
| ι.3 | 競合発生時の挙動 | **新 Outcome variant `ConflictLost` を追加**。articles INSERT を try/except IntegrityError で囲み、existing 検出で `ConflictLost` + audit `outcome_code='conflict_lost'` (SKIPPED)、なしで `TerminallyDropped('article_persist_anomaly')`。旧設計の「既存読み戻し → ContentFetched(既存) で chain」は不採用 (winner が chain 済) | **決定** |
| ι.4 | retry/exhausted 判定の責務 + dispatch 機構 | **Service に集約**: retry policy 適用 / DB 状態更新 / audit を Service が完結。**Service は kiq 一切しない** (extract_content.kiq も schedule_by_time も task の責務)。**dual-dispatch 撤去**: cron poller (1 分間隔) のみで再投入、broker schedule_by_time は不採用。`taskiq max_retries=0 + retry_on_error=False` で taskiq retry 機構を完全に殺す。policy table は `app/collection/extraction/retry_policy.py` に分離 | **決定** |
| ι.A | payload field rename | `discovered_article_id` → `article_url_id` (PR2.5-B 内、機械的) | **決定** |
| ι.B | 永続化 FK 切替 | `analyzable_articles.discovered_article_id` → `analyzable_articles.article_url_id` (PR2.5-B 内、機械的) | **決定** |
| ι.C | race-lost lookup 切替 | `find_by_discovered_article_id` → `find_by_article_url_id` (PR2.5-B 内、機械的) | **決定** |
| κ | Stage 1 audit | `SourceFetchPayload` を **経路非依存の `completion_*` 命名** で再定義。count fields は `int = 0` で常時 populate (invariant `entry_count == sum(...)` 検証可)。kind versioning なし、TRUNCATE pipeline_events で clean slate | **決定** |

---

## PR 段階分割 (cutover 方式、dual-write なし)

デプロイ前段階で問題の多い旧設計を引きずらない方針。dual-write 期間を設けず、cutover で新基盤に切り替える。

| PR | 内容 | rollback 性 |
|---|---|---|
| **PR2.5-A** (基盤) | alembic で `article_urls` / `pending_html_articles` 新設 + `analyzable_articles.article_url_id` (nullable) 追加。ORM 定義 + Pydantic schema (`StagedArticleAttributes`) + URL 正規化 utility + tests。**既存 analyzable_articles に対し article_url_id をバックフィル** (同 migration 内で実施)。behavior 変更ゼロ | 完全可逆 (テーブル追加のみ) |
| **PR2.5-B** (cutover、単一コヒーレント PR) | Fetcher の戻り値を `Ready / Failed / None` に変更、IngestionService が articles 直 INSERT or pending_html_articles INSERT へ振り分け、ContentFetchService が pending_html_articles 駆動に書き換え、extract_html_body の入力を `pending_html_article_id` に変更、cron poller (`dispatch_html_fetch_jobs`) + sweeper 投入、PR2 audit を `article_url_id` 参照に追従改修。**discovered_articles への書込は完全停止**、kiq 直駆動 path 撤去 | 不可逆 (運用切替) |
| **PR2.5-C** (legacy 撤去) | `DROP TABLE discovered_articles` + `StagedArticle` ORM 撤去 + `analyzable_articles.article_url_id` を `NOT NULL` 昇格 + `analyzable_articles.source_url` 列を削除 (article_urls 経由に統一) | 不可逆 (DROP) |
| **PR2.5-D** (retry 強化、必要なら) | per-error policy table の調整、`retry_policy.py` 拡充。dispatch 機構変更は含まない (cron only で確定) | 独立 |

### PR2.5-B の cutover 戦略

deploy 手順:

1. **deploy 直前**: kiq queue を drain (現在 in-flight な extract_html_body を完走させる)
2. **deploy 直前**: 以下のテーブルを TRUNCATE:
   - `discovered_articles` (旧 Pattern H 経路の作業領域、in-flight 想定が無くなる)
   - `pipeline_events` (旧 semantic の Stage 1 audit を破棄、新 `SourceFetchPayload` で記録再開)
3. **deploy**: PR2.5-B を merge + 全 worker / backend container を再起動
4. **deploy 後**: 次の Stage 1 cron で新 path 経由の処理が始まる、URL は再 discover される (RSS feed window 内なら自動回復)

### 旧 in-flight URL の扱い

PR2.5-B 切替時、discovered_articles 内の `article 未生成 URL` は捨てる:

- 該当 URL が今も RSS feed window 内にいれば → 次の Stage 1 cron で再 discover、新 path で処理 (実害なし)
- window 外に落ちていれば → 失われるが、デプロイ前 dev 環境 + 永続失敗として既に articles 化されなかった URL なので影響軽微

dual-write による救済はこの極稀な時間窓に対して割に合わない (実装/レビュー/運用観察コストが過剰)。

### PR2.5-C のタイミング

PR2.5-B と同 sprint で連続 merge 推奨。新 path に問題があれば PR2.5-B の段階で気付くため、運用観察期間を設ける必要は薄い。1-2 日の本番観察後に PR2.5-C を merge して legacy 撤去。
5. **PR2 audit の追従**: ContentFetchPayload の参照対象を pending_html_articles ベースに置換

---

## やらないこと (将来 PR)

- HTTP 429 (rate limit) ソース別ハンドリング (Retry-After 尊重) — 別軸の問題
- 失敗種別ごとの delay 分岐 (502/504 を 5 分、503 を 30 分等) — YAGNI、データ見てから
- `pipeline_events` の retention / archive — 別 PR
- admin UI で job の状態確認 / replay — 別 PR
