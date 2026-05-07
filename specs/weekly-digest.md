# Weekly Digest — 定期実行サーバーサイド AI エージェント

Vector に追加する週次定期実行エージェント機能の仕様。最終更新: 2026-05-07 (9 service 集約により worker-digest → `worker-insights` 同居、scheduler-digest → `scheduler` 同居に移行。broker は無変更)。

## 概要

「今週の注目」レポートをカテゴリ単位で自動生成し、ダッシュボードに表示する週次定期実行エージェント。投資助言ではなく情報集約のポジショニング。「ディープリサーチ (Perplexity / Gemini Deep Research / ChatGPT Deep Research / Claude Research) では構造的にできないこと」を差別化軸に据える。

## ポジショニング

- **情報集約サービス**(投資助言サービスではない)
- 「動向を集める、結果として投資にも役立つ」立て付け
- マーケコピーは「テック動向集約」寄り、「投資分析」を前面に出さない

## 差別化軸 — ディープリサーチでは構造的にできないこと

| 軸 | ディープリサーチ | Vector |
|---|---|---|
| 能動性 | ユーザーが質問してから動く | ユーザーが質問する前に動向を投げる(継続監視) |
| 時間軸 | 現在の web スナップショットのみ | 過去蓄積との時系列差分(論調変化、新規出現) |
| コーパス | web 全体を広く浅く | 7ソースを密に蓄積、テック × バイオ × 量子 × 暗号 × 日本のニッチクロス |
| 形式知化 | 毎回ゼロから | entity + topic + (将来) sentiment ラベル化済み |

採用基準: この4軸のうち最低1つを「ディープリサーチでは出せない情報」として活かす機能のみ採用。すべてディープリサーチで代替可能なものは作らない。

## アーキテクチャ — レイヤー分離

```
┌─ レイヤー1: 既存パイプライン ─────────────────┐
│ Article → Extract → Analyze → Embed         │
│ (個別記事レベル、既に実装済み)                │
└──────────────────────────────────────────────┘
                ↓
┌─ レイヤー2: 集計レイヤー (LLM 不要、決定論的バッチ) ┐
│ Part A (ユーザー可視化対象 + エージェント参照):    │
│  - 急上昇 entity (週次、カテゴリ別)              │
│  - 急上昇 topic (週次、カテゴリ別)               │
│  - 新規言及 entity (週次)                        │
│ Part B (エージェント専用知識ベース):              │
│  - entity / topic モメンタム時系列                │
│  - entity 間の共起ペア (関係性発見)               │
│  - (将来) sentiment 集計                         │
└──────────────────────────────────────────────┘
                ↓ (エージェントが tool 経由で参照)
┌─ レイヤー3: エージェントレイヤー (LLM) ────────────┐
│ - 集計データを材料に動向を分析・解釈              │
│ - 自然言語で「今週の注目」レポートを生成          │
│ - ハルシネーション防止: ファクトに必ず紐付け      │
└──────────────────────────────────────────────┘
                ↓
┌─ レイヤー4: ダッシュボード ────────────────────┐
│ - エージェント生成の自然言語レポート (主)        │
│ - Hot リスト (急上昇 entity/topic、新規言及)     │
└──────────────────────────────────────────────┘
```

## 採用する機能

### Phase 1A — データ整備 + Hot リスト (LLM 不要)

**目的**: LLM を被せる前に集計データの精度と UI 上での実用性を検証する。決定論的なデータ整備層を先行させ、フィードバックを取りやすくする。

- **データ層 — 保存時の表記正規化** (`EntityName` VO):
  - NFKC + 連続空白統合 + 前後空白除去 + 1-200 文字制約
  - **casing は保持**(AI が文脈で抽出した casing 情報を破壊しない / memory: `feedback_ai_extraction_casing.md`)
  - 検索・グルーピング用に `match_key` プロパティを提供(`str.lower()` 出力、SQL `LOWER()` と locale 一致。`casefold()` ではない)
  - **同義語判定 / 意味的統合は対象外**(別レイヤーの責務)
  - 既存データは backfill migration で整形(NFKC + 空白のみ、casing 触らない)
  - **原則**: データ収集と整形は別責任。VO に保存時整形を閉じ込め、抽出層は生データの取り込みに集中
- **集計関数 (`TrendsRepository`)**:
  - 既存 `article_analyses` + `article_entities` からの SQL 集計(2 段 JOIN、`extraction_id` 直結、`COUNT(DISTINCT extraction_id)` で名寄せ重複吸収、`GROUP BY (lower(name), type)`)
  - `get_trending_entities` / `get_trending_topics` / `get_new_entities`(週次、カテゴリ別)+ `count_source_analyses`
  - 内部的に `week_start: date` 引数を取る
  - **集計結果は snapshot として永続化**(下記「集計レイヤー設計」参照、API では集計しない)
- **Snapshot 永続化 (`weekly_trends_snapshots`)**:
  - 全カテゴリの集計結果を 1 週分まとめて 1 行に格納(JSONB)
  - 月曜 00:05 JST(UTC 日曜 15:05)に専用 cron が再生成
  - 手動投入用 CLI を `app/insights/snapshot/cli/generate_snapshot.py` に提供(初回投入 / 緊急再生成、`--force` / `--week=...`)
- **API 層 (CQRS 風 Service 分離)**:
  - `WeeklyTrendsSnapshotService`(Command 経路): cron / CLI から呼ぶ生成・永続化責務(`async_sessionmaker` DI / memory: `feedback_session_factory_di.md`)
  - `WeeklyTrendsQueryService`(Query 経路): API から呼ぶ snapshot 直読み専用(`AsyncSession` DI、集計しない、on-demand fallback しない / memory: `feedback_failure_visibility.md`)
  - `GET /api/v1/weekly-trends`(**クエリパラメータなし**、最新 snapshot を返却。snapshot 不在時は 200 + 空 categories[]、生成は別経路に分離)
- **フロントエンド**: 「今週の注目」ページの **Hot リスト部分のみ**
  - カテゴリセクションごとに 3 リスト(急上昇 entity / 急上昇 topic / 新規言及 entity)
  - 過去週切替 UI は持たない(YAGNI、必要になった Phase で追加)
- **インフラ**: **`worker-insights` (broker_digest) + `scheduler` (scheduler_digest)** を追加(LLM は使わないが broker を分離して既存パイプラインへの影響を遮断、9 service 集約後は `worker-insights` / `scheduler` container に同居)。新 API key 不要

完了基準: Hot リストが画面で見えて、entity / topic 集計の精度がレビューで許容範囲。

### Phase 1B — エージェントレポート (LLM 投入)

**目的**: Phase 1A で整備されたデータに LLM を被せ、カテゴリ単位の自然言語レポートを生成する。

- **アダプター**: `ReportGenerator` ポート + DeepSeek-V4 / Anthropic Haiku 4.5 両アダプター
- **ドメイン**:
  - `WeeklyDigestService`(集計取得 → 生成 → フィルタ → 永続化のオーケストレーション)
  - 投資助言語彙の後処理フィルタ(純粋関数)
- **インフラ**: `broker_agent` + `worker_agent` service、`ANTHROPIC_API_KEY` + `DEEPSEEK_API_KEY`、Pydantic Logfire 設定
- **データ**: `weekly_digest_reports` テーブル(Hot リスト snapshot を JSON 同梱、過去レポートの再現性確保)
- **フロントエンド**: 「今週の注目」ページのレポート部分追加

### Phase 2 以降 (Phase 1B のフィードバックを見て判断)

- モメンタム時系列(集計レイヤー Part B 拡張、エージェントの input に追加)
- エンティティ関係性発見(共起ペア集計、エージェントの input に追加)
- 論調シフト追跡(sentiment / stance ラベル列追加 + 過去再分析)
- エンティティ関係のネットワーク図 UI(オプション)

## やらない機能

- 集合 (Cluster Digest / 多ソース集約のみ) — ディープリサーチで代替可能
- ネットワーク (投資家マップ) — PitchBook / Crunchbase 主戦場で勝てない
- 外部リサーチ (能動収集) — ToS / CFAA リスク
- ad-hoc 質問対応 — ディープリサーチが既に解決している領域、戦わない
- 個人パーソナライズ / Watch List 機能 — 全ユーザー共通の集約のみ

## 設計上の確定事項 (2026-04-26)

| 項目 | 決定 |
|---|---|
| 出力場所 | ダッシュボード(メール / RSS は採用しない) |
| 検出対象 | entity + topic 両方 |
| 時系列単位 | 週次 |
| 個人パーソナライズ | 採用しない |
| レポート粒度 | カテゴリ単位 (既存 Vector の Category 体系を流用、「セクター」は別概念として導入しない) |
| レポート構成 | 1本のレポートにカテゴリセクションを並べる(MVP は単一ページ構成) |
| 集計データの位置づけ | Part A = ユーザー可視化 + エージェント参照 / Part B = エージェント専用 |
| MVP 戦略 | まず最小構成でリリース → フィードバックで改善する反復方針 |
| 集計の実装方式 | **snapshot 永続化**(`weekly_trends_snapshots` JSONB 1 行 / 週、cron で月曜 00:05 JST 生成、API は snapshot 直読み) |
| Service 構成 | **CQRS 風分離**(`SnapshotService` = Command 経路 / `QueryService` = Read 経路、別 DI 単位) |
| 集計バッチ実行系 | **`broker_digest` + `scheduler_digest`** を分離(9 service 集約後は `worker-insights` / `scheduler` container に同居、broker は独立) |
| 週の境界 | 月曜 00:00 JST 始まり (ISO 週) |
| 集計時刻軸 | `article_analyses.analyzed_at`(`published_at` は null 許容のため不採用) |
| 新規言及 entity 判定窓 | 過去 4 週(28 日)遡って 1 度も登場履歴がない |
| カテゴリ単位 | カテゴリ別に集計、横断はクエリで集約 |
| 集計対象 | Classified の article のみ(rejection は除外) |
| エージェント設計 | ポート/アダプター(ヘキサゴナル)、ドメイン層は LLM 詳細を知らない |
| モデル切替 | config + DI で provider/model 指定可能 |

## 技術スタック (2026-04-26 確定)

| カテゴリ | 採用 | 備考 |
|---|---|---|
| エージェントフレームワーク | **Pydantic AI v1** (2025-09 GA) | Pydantic v2 + SQLModel ネイティブ、Vector スタックと完全整合 |
| モデル抽象 | **ポート/アダプター(ヘキサゴナル)** | ドメイン層は `ReportGenerator` Protocol のみ依存、provider/model は config + DI で切替 |
| MVP モデル候補 | **DeepSeek-V4**(2026-04-24 リリース)、**Claude Haiku 4.5** | DeepSeek はコスト Anthropic 比 1/4〜1/5 想定。日本人向け / 個人情報なし / 機密情報なしのため地政学的懸念は最小化。/forge で出発点を確定 |
| 上位モデル候補 | Claude Sonnet 4.6($3 / $15)、Gemini Flash 2.5(独立性確保のためエージェントには採用しない) | レポート品質を見て切替判断 |
| プロバイダ | **Anthropic + DeepSeek**(両アダプター実装、デフォルトは config 切替) | 新規、既存 Gemini パイプラインとは独立 |
| 観測性 | **Pydantic Logfire** | 無料枠 10M spans/月、Pydantic AI と一体運用 (`logfire.configure()` 1 行) |
| broker 分離 | **レベル B** (broker + worker process 分離) | 専用 `broker_agent` + `worker_agent` service を docker-compose に追加 |
| API key 分離 | 新規 **`ANTHROPIC_API_KEY`** + **`DEEPSEEK_API_KEY`** | `config.py`、既存 `GEMINI_API_KEY` と独立 |
| コスト管理 | monthly budget cap + per-call token cap + kill switch | `config` に集約 |
| 集計バッチ broker | **専用 `broker_digest` + `scheduler_digest`**(Phase 1B の `worker_agent` とは独立) | LLM 不要だが既存パイプラインの broker と queue を分離して耐障害性確保。9 service 集約後は `worker-insights` / `scheduler` container に supervisord 同居 |

## 集計レイヤー設計 (2026-04-27 改訂)

### 集計の実装方針 — snapshot 永続化

集計結果は `weekly_trends_snapshots` テーブルに **週単位 1 行 (JSONB)** で永続化する。同じデータを全ユーザーが見る / リアルタイム性不要 / 毎リクエスト集計は無駄、という判断で都度計算方式から方針転換 (2026-04-26)。

```
weekly_trends_snapshots
├── week_start            DATE         PRIMARY KEY (ISO 週、月曜)
├── bundle                JSONB        全カテゴリのトレンド束 (WeeklyTrendsBundle)
├── generated_at          TIMESTAMPTZ  生成時刻
└── source_analysis_count INTEGER      集計元の article_analyses 件数 (監査用)
```

設計原則 (memory: `feedback_snapshot_responsibility.md`):

- **snapshot は 1 単位保存が責務**。推移分析や横断クエリのために `weekly_entity_trends` のような正規化テーブル群に分解しない
- **JSONB 1 カラム + メタ情報** が原則。`bundle` の構造は `WeeklyTrendsBundle` (frozen Pydantic) と 1:1
- `source_analysis_count` は再生成判断 / 監査用のメタ情報のみ。検索キーには使わない

### Service の責務分離 (CQRS 風)

snapshot の **生成** と **提供** は責務が異なるため別 Service に分離する (memory: `feedback_failure_visibility.md` — 故障の見える化、API endpoint の自己修復 fallback で隠蔽しない)。

| Service | 経路 | 依存 | 責務 |
|---|---|---|---|
| `WeeklyTrendsSnapshotService` | cron / CLI (Command) | `async_sessionmaker` (memory: `feedback_session_factory_di.md`) + `TrendsRepository` + `SnapshotsRepository` | 集計実行 → bundle 構築 → snapshot 永続化 (`insert_if_absent` または `upsert`) |
| `WeeklyTrendsQueryService` | API (Query) | `AsyncSession` + `SnapshotsRepository` | `find_latest()` のみ呼ぶ。集計しない / on-demand fallback しない |

API は snapshot が無ければ `weekStart=null` + 空 `categories[]` を返す。生成は cron / CLI に任せて API は読むだけ。

### Snapshot 生成の経路

| 経路 | 責任 | 引数 |
|---|---|---|
| 月曜 00:05 JST cron (UTC 日曜 15:05) | `app/insights/snapshot/tasks/snapshot.py` の `@broker.task` | なし (直近完了週を自動算出) |
| 手動 CLI | `app/insights/snapshot/cli/generate_snapshot.py` | `--week=YYYY-MM-DD` (省略時は直近完了週) / `--force` (既存上書き) |

CLI は **初回投入** (frontend デプロイ前に snapshot 1 件以上を確保) と **緊急再生成** (集計バグ修正後の再計算) に使う。

### 過去レポートの再現性 (Phase 1B)

Phase 1B の `weekly_digest_reports` (LLM レポート) は `weekly_trends_snapshots.week_start` を FK 参照する。snapshot を入力に LLM プロンプトを組み立てる設計。元データが変わっても snapshot が固定されているため、Phase 1B の再生成は決定論的。

### 急上昇 entity / topic の判定式

ハードフィルタ(ノイズ排除):

```
current_count >= MIN_CURRENT
AND (previous_count >= MIN_PREVIOUS
     OR current_count >= NEW_BURST_THRESHOLD)
```

スコア(smoothing 付き、降順 → top 20):

```
hotness_score = (current - previous) / max(previous, SMOOTHING)
```

| 定数 | 既定値 | 役割 |
|---|---|---|
| `MIN_CURRENT` | 5 | 今週の最小言及数(絶対数フィルタ) |
| `MIN_PREVIOUS` | 2 | 前週の最小値(微増ノイズ排除) |
| `NEW_BURST_THRESHOLD` | 10 | 0/微少からのバースト許可ライン |
| `SMOOTHING` | 2 | スコアの smoothing 定数(0 除算回避 + 過剰増幅抑制) |

これらは `config.py` に集約、データを見ながら調整可能(MVP 戦略「動かして改善」)。

例:

| 推移 | フィルタ | hotness_score | 順位 |
|---|---|---|---|
| 0 → 1 | 落ちる(`current < 5`) | — | — |
| 0 → 10 | 通過(`current >= 10`) | 5.0 | 上位 |
| 1 → 6 | 落ちる(`previous < 2` かつ `current < 10`) | — | — |
| 2 → 6 | 通過 | 2.0 | 中位 |
| 5 → 20 | 通過 | 3.0 | 上位 |
| 100 → 110 | 通過 | 0.1 | 下位 |

### 新規言及 entity

`(entity_name, entity_type)` の組で、過去 4 週(28 日)遡って 1 度も登場履歴がないものを「新規」と判定。今週の言及数で降順ランク → top 20。

`0 → N` の急増(`N >= NEW_BURST_THRESHOLD`)は急上昇テーブルにも登場(別性質のホット度として二重表示を許容)。

### 集計関数のインターフェース

```python
class TrendsRepository:
    async def get_trending_entities(
        self, category_id: int, week_start: date, limit: int = 20,
    ) -> list[EntityTrend]: ...

    async def get_trending_topics(
        self, category_id: int, week_start: date, limit: int = 20,
    ) -> list[TopicTrend]: ...

    async def get_new_entities(
        self, category_id: int, week_start: date, limit: int = 20,
    ) -> list[NewEntity]: ...
```

エージェント tool / Hot リスト API endpoint で共用。

## ドメイン設計 — ポート/アダプター (2026-04-26 確定)

ドメイン層がビジネスロジックを持ち、LLM の詳細を一切知らない構造。モデル切替は config + DI で完結。

### ディレクトリ構造 (Phase 1A 確定 / Phase 1B 拡張余地)

```
backend/app/insights/snapshot/
├── domain/
│   ├── trend.py             # [1A] EntityTrend / TopicTrend / NewEntity (末端 VO)
│   │                        #      + WeeklyCategoryTrends (集約ルート)
│   │                        #      + WeeklyTrendsBundle (snapshot 形)
│   ├── report.py            # [1B] ReportDraft (frozen Pydantic, 不変条件)
│   ├── digest_service.py    # [1B] WeeklyDigestService (ビジネス手順を組み立てる層)
│   └── ports.py             # [1B] ReportGenerator (Protocol) ← ポート
├── repository/
│   ├── trends.py            # [1A] TrendsRepository (集計 SQL × 3 + count_source_analyses)
│   └── snapshots.py         # [1A] SnapshotsRepository
│                            #      (find_latest / find_by_week / insert_if_absent / upsert)
├── application/
│   ├── snapshot.py          # [1A] WeeklyTrendsSnapshotService
│   │                        #      (Command 経路、async_sessionmaker DI)
│   └── query.py             # [1A] WeeklyTrendsQueryService
│                            #      (Query 経路、AsyncSession DI)
├── adapters/                # [1B] LLM プロバイダ別アダプター
│   ├── anthropic.py
│   ├── deepseek.py
│   └── (gemini.py)
├── tasks/
│   ├── broker.py            # [1A] 専用 broker_digest (queue 名 "digest")
│   └── snapshot.py          # [1A] @broker.task 月曜 00:05 JST cron
├── cli/
│   └── generate_snapshot.py # [1A] 手動 CLI (--force / --week=...)
├── router/
│   └── weekly_trends.py     # [1A] GET /api/v1/weekly-trends
├── schemas/
│   └── weekly_trends.py     # [1A] camelCase レスポンス schema
│                            #      (weekStart / weekEnd / generatedAt nullable)
├── filters/                 # [1B] 投資助言語彙の後処理フィルタ
│   └── investment_advice.py
└── config.py                # [1A] MIN_CURRENT 等の判定式定数を集約
```

`[1A]` = Phase 1A スコープ、`[1B]` = Phase 1B 追加。Phase 1A は LLM を使わないため `domain/ports.py` / `adapters/` / `filters/` は未作成のまま開始する。

### ポート定義(ドメイン層)

```python
class ReportGenerator(Protocol):
    async def generate(
        self,
        category: Category,
        trends: TrendBundle,  # 集計データを束ねた frozen Pydantic
    ) -> ReportDraft: ...
```

### サービス層

```python
class WeeklyDigestService:
    def __init__(
        self,
        session_factory: async_sessionmaker,  # memory: feedback_session_factory_di.md
        trends_repo: TrendsRepository,
        report_generator: ReportGenerator,    # ポート
        filter: InvestmentAdviceFilter,
    ): ...
```

ドメインは LLM を一切知らない(`ReportGenerator` ポートのみ依存)。テストは fake/stub 実装で完結。

### モデル切替 DI

```python
# config.py
AGENT_PROVIDER: Literal["anthropic", "deepseek"] = "deepseek"
AGENT_MODEL: str = "deepseek-v4"
```

FastAPI の `Depends` で provider を見て adapter を返す factory を 1 つ。

### モデル採用判断の前提

- 完全に日本人向けアプリ
- 個人情報を扱わない(ウォッチリスト程度のみ)
- 機密情報を扱わない(公開記事 + AI 生成集計のみ)

これにより地政学的懸念(B2B 顧客への説明、データガバナンス)はほぼ無効化。コストと品質で判断可能。

### モデル使用範囲ルール (2026-04-26 確定)

**DeepSeek-V4 は「ニュース本文(第三者著作物)を渡さない箇所のみ」で使用する。**

- 週次ダイジェストエージェントは集計データ + メタデータのみを input とするため、デフォルト provider は DeepSeek-V4
- 将来、ニュース本文を LLM に渡すユースケース(例: 個別記事の深掘り解説、本文要約、長文チャット応答など)が追加される場合は、**その箇所のみ Anthropic / Gemini など別 provider を使う**
- アダプター層でユースケースごとに provider を使い分けられる構成にしておく
- これによりコスト優位性(DeepSeek)と著作権配慮(本文を渡す箇所は他 provider)を両立

### MVP 出発点(/forge で確定)

| 案 | メリット | デメリット |
|---|---|---|
| A: DeepSeek-V4 単独 | コスト最低、現実品質を直接測定 | 品質ベースラインなし、原因切り分け困難、Pydantic AI 対応未完了(後述) |
| B: Anthropic Haiku 4.5 ベースライン + DeepSeek-V4 切替評価 | 品質比較可能、ポート/アダプター設計の検証も兼ねる | 初期コストやや高め |

### DeepSeek-V4 リサーチ結果 (2026-04-26 確認)

公式情報で以下が確認できた:

| 項目 | 内容 |
|---|---|
| リリース | 2026-04-24 **Preview Release**(GA タイムライン未公表) |
| モデル系統 | `deepseek-v4-pro` (1.6T total / 49B active) と `deepseek-v4-flash` (284B total / 13B active) |
| API | OpenAI 互換 (`api.deepseek.com`) + Anthropic 互換 (`api.deepseek.com/anthropic`) |
| Context / Max Output | 1M / 384K token |
| 価格 (V4-Flash) | input $0.14 / output $0.28 / cache hit $0.028 per 1M token |
| 価格 (V4-Pro) | input $0.435 / output $0.87(2026-05-05 まで 75% off の特別価格) |
| Tool / JSON Output | 公式対応 |
| ライセンス | MIT(weights のみ。API ToS は別) |

#### エージェント input の構成(2026-04-26 確定)

エージェントには **ニュース本文(第三者著作物)を渡さない**。input は Vector が生成・集計した以下のデータのみ:

- 集計データ(entity 名、topic 名、カテゴリ名、数値、growth_rate)
- 出典記事のメタデータ(タイトル、URL、公開日)
- 必要に応じて Vector 生成の summary / translated_title(派生物の取り扱いは要検討)

これにより「第三者著作物を LLM プロバイダの訓練に流す」リスクは構造的に発生しない。

#### 採用上の懸念点(リサーチで判明)

1. **データ利用規約**: DeepSeek 公式 ToS はユーザー入出力をサービス提供・改善・モデル訓練に利用する権利を保持、データは中国国内サーバー格納。
   - **Vector の用途では問題にならない**: ニュース本文を渡さず、集計済みデータ(数値・ラベル)とメタデータのみを流すため、第三者著作物の訓練流入は構造的に発生しない
   - 残る検討項目: Vector 生成の summary / translated_title を含める場合は派生物の扱いを要確認(MVP では要約類は含めず集計データのみで生成可能か検証)

2. **Pydantic AI v1 サポート未完了**: GitHub Issue #5193 がオープン中。`deepseek-v4-*` を明示指定すると `tool_choice='required'` の非互換で HTTP 400。暫定回避は `deepseek-chat` 透過ルーティングだが **2026-07-24 で完全廃止予定**。
   - **ポート/アダプター設計で回避可能**: `DeepSeekReportGenerator` を OpenAI SDK + `base_url='https://api.deepseek.com'` で直接実装すれば Pydantic AI Issue に依存しない。Anthropic アダプターのみ Pydantic AI v1 を使う非対称構成でもポート設計上は問題なし

3. **Preview Release**: 安定性 SLA 未公表、GA 移行スケジュール未定。
   - 対応: 両アダプターを用意し、DeepSeek 障害時は config 1 行で Anthropic に即フェイルオーバー

#### Anthropic Haiku 4.5 との比較

| 観点 | DeepSeek-V4-Flash | Claude Haiku 4.5 |
|---|---|---|
| input / output 単価 | $0.14 / $0.28 | $1.00 / $5.00 |
| コスト比 | 1x | ~7-18x |
| データ利用規約 | 訓練利用権利あり、中国サーバー | 訓練に使わない明示、Anthropic policy |
| Pydantic AI v1 対応 | 未完了(Issue #5193) | 完了(native) |
| リリース成熟度 | Preview | GA |

#### 推奨(リサーチ結果反映)

**MVP は両アダプター実装 + DeepSeek-V4 をデフォルト provider** に設定。Anthropic Haiku 4.5 はベースライン / フェイルオーバー用に常時使える状態にしておく。理由:

- ニュース本文を渡さない設計が確定したので、DeepSeek の規約懸念は無効化
- Pydantic AI Issue #5193 は OpenAI SDK 直接呼び出しで回避可能、ポート/アダプター抽象が吸収
- DeepSeek Preview の安定性リスクは「Anthropic への即時切替可能」設計でヘッジ
- コスト優位性(Haiku 4.5 比 1/7〜1/18)を活かせる

実装上の構成:

| アダプター | 内部 SDK | 用途 |
|---|---|---|
| `DeepSeekReportGenerator` | OpenAI SDK + `base_url=api.deepseek.com` | デフォルト(本番) |
| `AnthropicReportGenerator` | Pydantic AI v1 (native Anthropic) | ベンチマーク / フェイルオーバー / 品質比較 |

切替は `config.AGENT_PROVIDER` 1 行。両アダプターの実装コストはポート設計のおかげで限定的。

## 法務対応の核

- Buy/Sell/上昇/下落/推奨など投資助言的な語彙を機械的に禁止 (LLM プロンプト + 後処理フィルタ)
- アラート(push)ではなくレポート(pull)形式 → 「継続的シグナル配信」性を構造的に下げる
- マーケコピーは「テック動向集約」寄り、「投資分析」を前面に出さない
- Yahoo Finance ソースは既に運用停止済み (Compass legal の NoGo 懸念はクローズ)

## 今後取り組むこと (TODO)

### A. 技術選定 (大半確定 2026-04-26、残: 集計レイヤー DB / コスト管理具体)

- [x] エージェントフレームワーク選定 → **Pydantic AI v1**
- [x] レポート生成モデル選定 → **Claude Haiku 4.5** (MVP)、品質次第で Sonnet 4.6 にアップグレード判断
- [x] scheduler 組み込み → **専用 broker_agent + worker_agent (レベル B 分離)**
- [x] 専用 AI API key → 新規 **`ANTHROPIC_API_KEY`** を `config.py` に追加
- [x] 観測性スタック選定 → **Pydantic Logfire** (無料枠で十分)
- [x] 集計レイヤー DB 設計 → **集計テーブルなし、クエリ都度計算で確定**(必要なら後追いで MATERIALIZED VIEW)
- [x] 急上昇判定式 → **フィルタ + smoothing スコア**(MIN_CURRENT=5 / MIN_PREVIOUS=2 / NEW_BURST_THRESHOLD=10 / SMOOTHING=2)
- [x] エージェント設計方針 → **ポート/アダプター(ヘキサゴナル)、モデル切替は config + DI**
- [x] DeepSeek-V4 リサーチ → **Preview リリース実在、コスト 1/7〜1/18。ニュース本文を渡さない設計で訓練利用懸念は無効化、Pydantic AI Issue #5193 は OpenAI SDK 直接呼び出しで回避**
- [x] MVP 出発点 → **両アダプター実装 + DeepSeek-V4 デフォルト、Anthropic Haiku 4.5 はベンチマーク / フェイルオーバー**
- [ ] エージェント input の最終構成確定(集計データ + メタデータのみで品質確保できるか、要約類含めるか検証)
- [ ] コスト上限ガード / kill switch / 冪等キー / retention の具体設計
- [ ] /forge での実装プラン策定(ディレクトリ構造、クラス分割の最終確定)

### B. Phase 1A — データ整備 + Hot リスト (LLM 不要)

実装プランは `plans/drafts/20260426-095922/PLAN.md` に詳細、PR 5 段階分割の着手順序は `plans/<branch>` のロードマップに従う。

**PR-A — EntityName VO 改修 + auth retrofit**

- [ ] `EntityName` VO に NFKC + 連続空白統合 + `match_key` (`str.lower()`) を追加 (casing は保持、1-200 文字制約は維持)
- [ ] `categories.py` / `articles.py` router に `Depends(get_optional_user)` を追加 (認可境界の一貫性)

**PR-B — entity 名 backfill migration** (PR-A デプロイ + 24h 監視後、Ask first)

- [ ] T2-pre 監査 SQL で影響行数 / 重複候補を計測し PR description に記録
- [ ] Alembic migration で `article_entities.name` を NFKC + 空白整形 (casing 触らない、適用後 0 件残ることを assert)

**PR-C — snapshot 基盤** (Ask first: `docker-compose.yml` 編集 + 本番 CLI 実行)

- [ ] `weekly_trends_snapshots` テーブル + Alembic migration (week_start PK, bundle JSONB, generated_at, source_analysis_count)
- [ ] `app/insights/snapshot/domain/trend.py` — `EntityTrend` / `TopicTrend` / `NewEntity` (末端 VO) + `WeeklyCategoryTrends` (集約ルート) + `WeeklyTrendsBundle` (snapshot 形)
- [ ] `app/insights/snapshot/repository/trends.py` — 3 集計 SQL + `count_source_analyses`
- [ ] `app/insights/snapshot/repository/snapshots.py` — `find_latest` / `find_by_week` / `insert_if_absent` / `upsert`
- [ ] `app/insights/snapshot/config.py` — `MIN_CURRENT=5` / `MIN_PREVIOUS=2` / `NEW_BURST_THRESHOLD=10` / `SMOOTHING=2` 等の定数集約
- [ ] `app/insights/snapshot/application/snapshot.py` — `WeeklyTrendsSnapshotService` (Command 経路)
- [ ] `app/insights/snapshot/cli/generate_snapshot.py` — 手動 CLI (`--force` / `--week=...`)
- [ ] `app/insights/snapshot/tasks/broker.py` + `tasks/snapshot.py` — 専用 broker_digest + 月曜 00:05 JST cron
- [ ] `docker-compose.yml` の `worker-insights` (Procfile.insights) + `scheduler` (Procfile.scheduler) に digest broker / scheduler を追加 (9 service 集約後の同居方式、`brokers.py` の既存パターン参考)
- [ ] PR-C デプロイ後に CLI で初回 snapshot を投入 (frontend デプロイ前の必須前作業)

**PR-D — Query 経路** (PR-C 完了後)

- [ ] `app/insights/snapshot/application/query.py` — `WeeklyTrendsQueryService` (`AsyncSession` DI、`find_latest()` 専用)
- [ ] `app/insights/snapshot/router/weekly_trends.py` — `GET /api/v1/weekly-trends` (`Depends(get_optional_user)`)
- [ ] `app/insights/snapshot/schemas/weekly_trends.py` — camelCase レスポンス schema (`weekStart`/`weekEnd`/`generatedAt` nullable)
- [ ] `backend/app/main.py` に router 登録
- [ ] E2E テスト (snapshot 不在 → 200 + 空 / snapshot 在 / 複数週)

**PR-E — frontend page** (PR-D デプロイ + 初回 snapshot 投入後)

- [ ] `frontend/src/app/(protected)/weekly-trends/{page,loading,error}.tsx` (Server Component, ISR `revalidate: 86400`)
- [ ] `frontend/src/components/weekly-trends/{HotEntityList,HotTopicList,NewEntityList}.tsx`
- [ ] `frontend/src/lib/api-client.ts` に `getWeeklyTrends()` 追加
- [ ] `Header.tsx` に nav link 追加
- [ ] `npm run generate-types` で型再生成 (/gen-types skill)

**横断**

- [ ] entity / topic 集計の精度検証 (snapshot 投入後に bundle を実データでレビュー)
- [ ] `make pipeline-status` で `worker-insights` + `scheduler` が見えることを確認 (Makefile 側の `WORKERS` 変数は本 9 service 集約で `worker-insights` / `scheduler` 単一名に集約済み)

### C. Phase 1B — エージェントレポート (LLM 投入)

- [ ] `ANTHROPIC_API_KEY` + `DEEPSEEK_API_KEY` + `AGENT_PROVIDER` + `AGENT_MODEL` を `config.py` に追加(`.env.example` も更新)
- [ ] `broker_agent` + `worker_agent` service 追加(`docker-compose.yml`)
- [ ] Pydantic Logfire 設定(`logfire.configure()` を起動時に呼ぶ)
- [ ] `app/insights/snapshot/` を Phase 1B スコープに拡張(`domain/ports.py`、`domain/digest_service.py`、`domain/report.py`、`adapters/`、`filters/`、`tasks.py`)
- [ ] `ReportGenerator` ポート定義(Protocol)
- [ ] `AnthropicReportGenerator` アダプター実装(Pydantic AI v1)
- [ ] `DeepSeekReportGenerator` アダプター実装(OpenAI SDK 直接呼び出し、`base_url=api.deepseek.com`)
- [ ] `WeeklyDigestService` 実装(集計取得 → 生成 → フィルタ → 永続化のオーケストレーション)
- [ ] 投資助言語彙の後処理フィルタ(純粋関数)
- [ ] `weekly_digest_reports` テーブル + Alembic migration(Hot リスト snapshot を JSON 同梱)
- [ ] 出典記事リンク併記の構造的強制(レポートスキーマで必須化)
- [ ] taskiq 週次バッチタスク(`broker_agent`)
- [ ] 「今週の注目」ページのレポート部分追加

### D. Phase 2 以降 (Phase 1B リリース後の判断事項)

- [ ] モメンタム時系列の集計関数 + エージェントの input に追加
- [ ] entity 間の共起ペア集計 + エージェントの input に追加
- [ ] sentiment / stance ラベル列追加 + 過去再分析の範囲決定
- [ ] エージェントの分析角度を増やす (関係性解釈、論調変化の意味付け)
- [ ] エンティティ関係のネットワーク図 UI (オプション、価値検証後)

### E. 並行で別タスクとして実施

- [ ] 既存の全文 AI 翻訳機能の法務整備 (ライセンス契約 or 表示変更) — Compass legal が指摘した最大リスク

## 参考リンク

- Compass 多視点議論: `/Users/you/Vector/discussions/drafts/20260425-195516/DISCUSSION.md`
- Memory: `project_vector_agent_principles.md` (機能追加の二大原則)、`project_vector_agent_features.md` (本機能方針)
