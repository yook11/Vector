# fetch_log.py — VO / Annotated 型レビュー

## 対象モデル

`backend/app/models/fetch_log.py` (DeclarativeBase)

## フィールド一覧と判断

| # | フィールド | 現在の型 | nullable | 判断 | 理由 |
|---|---|---|---|---|---|
| 1 | `id` | `int` | PK | 変更不要 | 自動採番 |
| 2 | `source_id` | `int` | NOT NULL, FK | 変更不要 | FK + CASCADE + index で保護済み |
| 3 | `status` | `FetchStatus` (StrEnum) | NOT NULL | **CHECK 制約追加** | Enum の DB 層防御 |
| 4 | `articles_count` | `int` | NOT NULL, server_default=0 | **CHECK 制約追加** | 負数は不正 |
| 5 | `error_message` | `str \| None` (Text) | NULL | 変更不要 | nullable 自由テキスト |
| 6 | `duration_ms` | `int \| None` | NULL | **CHECK 制約追加** | 負数は不正 |
| 7 | `fetched_at` | `datetime` (TZ) | NOT NULL, server_default | 変更不要 | server_default=func.now() で DB に委譲済み |

## 判断基準の適用

### VO が不要な理由

FetchLog は **内部運用ログ** であり、API にも公開されていない。
作成パスは `news_fetcher.py:fetch_news_for_sources()` の **1箇所のみ**。
複数文脈で同じバリデーション/正規化が必要になるフィールドが存在しない。

| 基準 | 該当フィールド | → |
|---|---|---|
| 複数箇所で同じバリデーション/正規化が必要 | **なし** | — |
| URL フィールド | **なし** | — |
| 単一パスでしか作られない | 全フィールド | VO 不要 |

### Annotated 型も不要な理由

- `status`: StrEnum で型安全。CHECK 制約で DB 層を防御すれば十分
- `articles_count` / `duration_ms`: 単純な数値。非負制約は CHECK で十分
- `error_message`: 自由テキスト。フォーマット制約を課す意味がない

→ **DB CHECK 制約のみで十分**

## 各フィールドの詳細

### status — CHECK 制約追加

- **現状**: `FetchStatus(StrEnum)` で Python 層は型安全だが、DB 層には制約なし
- **作成パス**: `news_fetcher.py:313-315` — `FetchStatus.SUCCESS` / `FetchStatus.ERROR`
- **読み取りパス**: `source_helpers.py:29` (last successful fetch), `alpha_vantage.py:57` (daily quota)
- **対策**: DB CHECK `status IN ('success', 'error')`

### articles_count — CHECK 制約追加

- **現状**: `server_default=0` だが負数の防止なし
- **作成パス**: `news_fetcher.py:316` — `source_result.new_count`
- **読み取りパス**: テストアサーションのみ
- **対策**: DB CHECK `articles_count >= 0`

### duration_ms — CHECK 制約追加

- **現状**: nullable だが負数の防止なし
- **作成パス**: `news_fetcher.py:318` — `int((time.monotonic() - start) * 1000)` で計算
- **読み取りパス**: テストアサーションのみ
- **対策**: DB CHECK `duration_ms IS NULL OR duration_ms >= 0`

### 変更不要フィールドの理由

- `id`: PK 自動採番
- `source_id`: FK + CASCADE + index で保護済み
- `error_message`: nullable 自由テキスト。エラー内容は多様で制約を課す意味がない
- `fetched_at`: server_default=func.now() で DB に委譲。アプリ側で明示設定するケースもあるが値の制約は不要

## 追加する CHECK 制約

```sql
ALTER TABLE fetch_logs ADD CONSTRAINT ck_fetch_logs_status
  CHECK (status IN ('success', 'error'));

ALTER TABLE fetch_logs ADD CONSTRAINT ck_fetch_logs_articles_count_non_negative
  CHECK (articles_count >= 0);

ALTER TABLE fetch_logs ADD CONSTRAINT ck_fetch_logs_duration_ms_non_negative
  CHECK (duration_ms IS NULL OR duration_ms >= 0);
```

## スコープ

### この Phase で実施すること
- CHECK 制約 3 つをモデル定義 + Alembic マイグレーションで追加

### この Phase では実施しないこと
- FetchLog 用 API エンドポイント追加（現時点では内部使用のみ）
- `error_message` のサニタイズ — 現在 `news_fetcher.py` で `f"HTTP {resp.status_code}"` や例外メッセージをそのまま格納している。外部 API のレスポンスボディや URL に機密情報（API キー、トークン等）が含まれる可能性がある。API 公開やログ表示の実装前に、格納内容のサニタイズ方針を策定すること
- `fetched_at` の server_default 問題の調査（別途対応）

## 結論

- **VO 対象**: なし（内部運用ログ、作成パス 1 箇所、URL フィールドなし）
- **CHECK 制約追加**: `status`, `articles_count`, `duration_ms` の 3 フィールド
- **変更不要**: `id`, `source_id`, `error_message`, `fetched_at`
