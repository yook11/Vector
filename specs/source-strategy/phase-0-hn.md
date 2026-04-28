# Phase 0: Hacker News 登録漏れの解消

`phase-plan.md` の「Phase 1a: 既存維持5本」に Hacker News が含まれているが、
実装上の不整合で **未稼働** になっている。最少コストで解消し、副次的に
増分フェッチロジックの構造的バグも同時に修正する。

## 背景: spec と実装の不整合

| 観点 | 現状 |
|---|---|
| `app/collection/ingestion/fetchers/hacker_news.py` | 実装済み (Algolia HN Search API クライアント) |
| `app/collection/ingestion/registry.py:75` | `SourceName("Hacker News"): HackerNewsFetcher()` 登録済み |
| `app/config.py:62-64` | `hn_api_base_url` / `hn_min_points=20` / `hn_hits_per_page=50` 定義済み |
| `news_sources` テーブル | **行が存在しない** (`SELECT * WHERE name LIKE '%acker%'` → 0 rows) |
| `alembic/versions/*` | HN を INSERT する migration が **過去に一度も無い** |

スケジューラの `dispatch_sources` は `news_sources` の `is_active = true` 行を
ループするため、行が無い HN は呼び出されない。フェッチャー側は完成しているのに
起動経路が DB レベルで欠落しているという状態。

## 既存フェッチャーロジックの構造的バグ

`hacker_news.py:107-126` の増分フェッチが、**HN で典型的な
「徐々に伸びるストーリー」を構造的に取りこぼす**:

```python
last_fetched = await get_last_fetched_at(source.id)  # Redis 状態
since_timestamp = int(last_fetched.timestamp()) if last_fetched else None
# Algolia フィルタ: numericFilters="points>20,created_at_i>{since_timestamp}"
```

挙動を 30 分サイクルで追うと:

| 時刻 | since_timestamp | 09:00 投稿 (points が時間経過で 5 → 25 と成長) |
|---|---|---|
| 09:30 fetch | 09:00 | 09:00 投稿 (points=5) → `points>20` で除外 |
| 10:00 fetch | 09:30 | `created_at_i>09:30` で除外 (時刻フィルタで弾かれる) |
| 10:30 〜 | 10:00 〜 | ずっと `created_at_i` で除外 |
| 12:00 (points=25) | 11:30 | **永久に拾えない** |

つまり「投稿直後に既に 20 点超え」のストーリーだけが入ってくる。HN の
ranking が時間経過に依存することと相性が悪く、強い signal を取りこぼす。

## 修正方針: sliding window

`since_timestamp` 増分を捨て、**毎サイクル直近 24h の `points>20` を全部取得**
する設計に変更する:

- フィルタ: `numericFilters="points>20,created_at_i>{now - 86400}"`
- Redis state (`hn_fetch_state.py` の `get_last_fetched_at` /
  `set_last_fetched_at`) は不要 → ファイル削除
- dedup は既存の `discovered_articles.UNIQUE(original_url)` +
  `ON CONFLICT DO NOTHING` で構造排除
- 毎サイクル 50-100 hits 返るが `new_count` は実際の新規分のみ → ITmedia AI+ と
  同じ振る舞い (`candidates_count > 0` かつ大半 `new_count = 0`)

得られる構造的利点:

- **Slow-maturing story が 24h 以内に閾値を超えれば必ず捕捉される**
- Redis state とフェッチャーの同期漏れリスクがゼロ (state がそもそも無い)
- フェッチャーが純粋関数化され、テストが時刻 mock のみで完結

## hits_per_page の引き上げ

実測 (`verification-2026-04-27.md` 5 節): 19.5h で 50 hits = 24h で ~60 stories。
現状 `hn_hits_per_page=50` は 24h sliding window でオーバーフローする可能性が
高い。

- → **`hn_hits_per_page=100`** に引き上げ (Algolia は 1000 まで OK)
- 万一それでも溢れる日は **`hn_min_points=30`** に閾値を上げて対処 (config
  経由で env 切替可能)

## URL ホスト pre-filter は **入れない**

HN は外部リンク aggregator のため、Trafilatura 不適合 URL (GitHub README /
X post / YouTube / arXiv PDF / Pastebin / 動画/PDF) を含む。

設計判断:

- **採用しない (推奨)**: pre-filter を fetcher に組み込むと、fetcher の責務が
  「正規化された候補を返す」から「候補の品質判定」に肥大化し、
  `feedback_responsibility_by_purpose` に反する
- **採用する案**: ホスト別 reject ルールを fetcher 内に持つ → 観測前提の
  ルール化は `feedback_verify_before_fallback` に反する

採用する設計: **何も filter せず、既存の extraction → `article_rejections`
経路に任せて reason 別 rejection 件数を観察する**。
`feedback_failure_visibility` (故障の見える化) と
`feedback_verify_before_fallback` (実データで検証してからフォールバック) が
両方この方針を支持。

1 週間運用後に rejection 比率を見て、ホスト別ルールが必要かを再判断する。

## 重複の見え方

HN は外部リンクの aggregator なので、TechCrunch / VentureBeat / Krebs などの
記事が HN 経由で重複する。これは `ON CONFLICT DO NOTHING` で構造排除される
ため新規行は増えない。

ただし「HN 経由で来た記事の `news_source_id` が HN になる」のではなく、
**先に discover した側の `news_source_id` で固定される**。後から HN がそれを
feed しても skip されるだけで、HN を経由した signal は記録されない。

これは現スキーマの設計上の既定 (UNIQUE は `original_url` のみ)。HN 経由
かどうかの side-channel を持ちたい場合は副次キーや経由ログテーブルが必要に
なるが、本 Phase のスコープ外。

## 実装スコープ (1 PR)

1. **Alembic migration**: `news_sources` への `Hacker News` 行 INSERT
   - `name='Hacker News'`, `source_type='api'`,
     `endpoint_url='https://hn.algolia.com/api/v1'`,
     `site_url='https://news.ycombinator.com'`, `is_active=true`
2. **`hacker_news.py` 修正**: `since_timestamp` を `now - 86400` の
   sliding window に置換、`get_last_fetched_at` / `set_last_fetched_at`
   呼び出しを除去
3. **`hn_fetch_state.py` 削除**: 利用箇所が `hacker_news.py` のみであることを
   確認した上で
4. **`config.py` / `.env.example` 更新**: `hn_hits_per_page=50 → 100`
5. **テスト追加**:
   - sliding window が「投稿後に points が伸びたストーリー」を捕捉すること
   - 同一ストーリーが連続 fetch しても dedup されること (`ON CONFLICT` 経由)
   - `points<=20` のストーリーは Algolia 側で除外されること (フィルタ確認)
6. **spec 更新**: `phase-plan.md:10` の「Hacker News: 既存フェッチャー稼働中」
   を「Phase 0 で sliding window 設計に修正、初投入」に書き換え

## 受け入れ基準 (DoD)

- ruff / pytest pass
- `worker-metadata` 再起動後、最初の `dispatch_sources` 発火で
  `source_fetch_completed candidates_count=N new_count=N` (N≈60、初回のみ
  全件新規) を確認
- 30 分後の 2 回目 fetch で `candidates_count=N new_count≈0` (大半 dedup)
- 1 サイクルで HN 経由の `discovered_articles` が 60 件前後増えていること
  (DB 直接確認)

## 観測項目 (運用 1 週間)

- Net new discovery / day (HN 寄与分): 期待 +20-30/day
- HN 経由 article の Stage 2a (extraction) rejection 率と reason 内訳:
  GitHub / X / YouTube / PDF などの host 分布
- Stage 2b (classification) で `unsupported domain` 系の reject が
  増えていないか
- Algolia API のレスポンスタイム / エラー率: `points>20` で日量が大きく揺れる
  日 (バーストデー) に hitsPerPage=100 で十分か

## 関連ドキュメント

- 戦略: `README.md`, `phase-plan.md`
- スコアリング: `scoring.md`
- 旧検証: `verification.md` (2026-04-22)
- 新検証: `verification-2026-04-27.md`
- フェッチャー実装: `backend/app/collection/ingestion/fetchers/hacker_news.py`
- レジストリ: `backend/app/collection/ingestion/registry.py`
- パイプライン全体停滞診断: `specs/pipeline-stall-diagnosis-2026-04-26.md`
