# news_source.py — VO / Annotated 型レビュー

## 対象モデル

`backend/app/models/news_source.py` (DeclarativeBase)

## フィールド一覧と判断

| # | フィールド | 現在の型 | nullable | 判断 | 理由 |
|---|---|---|---|---|---|
| 1 | `id` | `int` | PK | 変更不要 | 自動採番 |
| 2 | `name` | `str` (max 50) | NOT NULL | **CHECK 制約追加** | 空文字列を防ぐ CHECK。regex はスキーマ層で十分 |
| 3 | `source_type` | `SourceType` (StrEnum) | NOT NULL | 変更不要 | Enum + 既存 CHECK `IN ('rss', 'api')` で保護済み |
| 4 | `site_url` | `str` (max 2048) | NOT NULL | **SafeUrl VO 適用** | URL 形式のフィールドには一律 SafeUrl を適用（多層防御） |
| 5 | `endpoint_url` | `str` (max 2048) | NOT NULL, UNIQUE | **SafeUrl VO 適用** | 同上。フェッチャーが HTTP GET に直接使用する |
| 6 | `is_active` | `bool` | NOT NULL | 変更不要 | boolean + server_default=true |
| 7 | `created_at` | `datetime` (TZ) | NOT NULL | 変更不要 | server_default=func.now() |
| 8 | `updated_at` | `datetime` (TZ) | NOT NULL | 変更不要 | DB トリガー `trg_news_sources_updated_at` で自動更新 |

## 判断基準の適用

### SafeUrl VO を適用する理由

URL 形式を取るフィールドには、作成パスの数に関係なく SafeUrl VO を一律適用する。
他レイヤーの検証（スキーマ層の `validate_url_scheme`、DB の CHECK 制約）を信用せず、**モデル層でも型レベルで URL の有効性を保証する（多層防御）**。

| フィールド | 既存の保護 | SafeUrl 追加で得られるもの |
|---|---|---|
| `site_url` | CHECK `~ '^https?://.+'` + スキーマ `validate_url_scheme()` | モデル層での型レベル保証 |
| `endpoint_url` | CHECK + UNIQUE + スキーマ | 同上 + フェッチャーが使う URL の型安全性 |

### VO が不要なフィールド

| 基準 | 該当フィールド | 理由 |
|---|---|---|
| Enum + CHECK で保護済み | `source_type` | `SourceType(StrEnum)` で十分 |
| boolean / server_default | `is_active`, `created_at` | 型や DB デフォルトで十分 |
| DB トリガー | `updated_at` | `trg_news_sources_updated_at` で自動更新 |
| PK / 自動採番 | `id` | — |

### Annotated 型が不要な理由

- `name`: regex パターン (`^(?=.*\w)[\w \-\.]+$`) はスキーマ層のみで使用。XSS 対策の一環であり、型レベルの制約として他で再利用する場面がない

## 各フィールドの詳細

### name — CHECK 制約追加

- **現状の問題**: NOT NULL だが空文字列 `""` を通してしまう
- **作成パス**: `routers/news_sources.py:69-89` — `NewsSourceCreate` スキーマで `min_length=1`, `strip()`, regex 検証
- **更新パス**: `routers/news_sources.py:92-117` — `NewsSourceUpdate` スキーマで同一検証
- **読み取りパス**: `routers/news_sources.py` (レスポンス), フェッチャー各種 (ログ出力用)
- **対策**: DB CHECK `name != ''`

### site_url — SafeUrl VO 適用

- **既存の保護**: DB CHECK `site_url ~ '^https?://.+'` + スキーマ `validate_url_scheme()`
- **作成パス**: `routers/news_sources.py` — `NewsSourceCreate.validate_site_url()` で検証
- **更新パス**: `routers/news_sources.py` — `NewsSourceUpdate.validate_site_url()` で検証
- **読み取りパス**: `routers/news_sources.py` — API レスポンスのみ（フェッチャーでは未使用）
- **対策**: `Mapped[SafeUrl]` に変更。TypeDecorator 経由で DB 読み書き時に SafeUrl バリデーション

### endpoint_url — SafeUrl VO 適用

- **既存の保護**: DB CHECK `endpoint_url ~ '^https?://.+'` + UNIQUE + スキーマ `validate_url_scheme()`
- **作成パス**: `routers/news_sources.py` — `NewsSourceCreate.validate_endpoint_url()` で検証
- **更新パス**: `routers/news_sources.py` — `NewsSourceUpdate.validate_endpoint_url()` で検証
- **読み取りパス**:
  - `news_fetcher.py:114` — RSS フェッチで HTTP GET のリクエスト先
  - `news_fetcher.py:267` — `urlparse(source.endpoint_url).hostname` でドメイン判定 → HN/AV 振り分け
  - `routers/news_sources.py` — API レスポンス
- **対策**: `Mapped[SafeUrl]` に変更。フェッチャーが使用する URL が型レベルで有効であることを保証

### source_type — 変更不要

- **既存の保護**: `SourceType(StrEnum)` + DB CHECK `source_type IN ('rss', 'api')`
- **作成パス**: `routers/news_sources.py` — Enum バリデーション済み
- **読み取りパス**: `news_fetcher.py:263` — RSS/API ルーティングの分岐条件

### updated_at — 変更不要（DB トリガー）

- **既存の保護**: `server_default=func.now()` + DB トリガー `trg_news_sources_updated_at`
- **トリガー**: `BEFORE UPDATE ON news_sources` で `NEW.updated_at = now()` を自動設定
- **現状の冗長コード**: ルーターの `source.updated_at = datetime.now(UTC)` (L113, L158) はトリガーが上書きするため冗長。SafeUrl 適用時に削除を検討

### 変更不要フィールドの理由

- `is_active`: boolean。`server_default=sa.true()`。toggle エンドポイントで反転のみ
- `created_at`: `server_default=func.now()`。アプリ側で設定しない

## SafeUrl 適用時の影響範囲

### モデル層

```python
# news_source.py
from app.domain import SafeUrl

site_url: Mapped[SafeUrl] = mapped_column(String(2048))
endpoint_url: Mapped[SafeUrl] = mapped_column(String(2048), unique=True)
```

TypeDecorator (`SafeUrlType`) + `type_annotation_map` は `news_article.py` の SafeUrl 適用時に設定済み。

### ルーター層

`routers/news_sources.py` での `NewsSource` 作成・更新時に `SafeUrl(url_str)` への変換が必要:

```python
# 作成時
source = NewsSource(
    name=data.name,
    source_type=data.source_type,
    site_url=SafeUrl(data.site_url),
    endpoint_url=SafeUrl(data.endpoint_url),
)

# 更新時 — SafeUrl 変換 + updated_at 手動設定の削除
if data.site_url is not None:
    source.site_url = SafeUrl(data.site_url)
if data.endpoint_url is not None:
    source.endpoint_url = SafeUrl(data.endpoint_url)
```

### スキーマ層（今回はスコープ外）

`schemas/news_source.py` の `NewsSourceResponse.site_url` / `endpoint_url` は現在 `str` のまま。
SafeUrl VO 適用後、ルーターで一時的に `str(source.site_url)` 等の変換が必要になる（`news_article.py` と同一パターン）。

**この str 変換問題はスキーマ層の見直しフェーズで解消する。**
全モデルの VO 見直しが完了した後、スキーマ層を SafeUrl に対応させることで `str()` 変換を不要にする。
今回は models 層の変更に集中し、ルーター/スキーマ側は暫定的な `str()` 変換 + TODO コメントで対応する。

### フェッチャー層

`news_fetcher.py` で `source.endpoint_url` を使用する箇所:
- `str(source.endpoint_url)` への変換が必要（HTTP GET の URL 引数は str）
- `urlparse(str(source.endpoint_url))` への変換が必要
- こちらもスキーマ層と同様、暫定的な `str()` 変換 + TODO コメントで対応

## 追加する CHECK 制約

```sql
ALTER TABLE news_sources ADD CONSTRAINT ck_news_sources_name_not_empty
  CHECK (name != '');
```

**注**: `site_url` / `endpoint_url` の CHECK 制約 (`~ '^https?://.+'`) は既存。SafeUrl VO と CHECK の両方で保護する。

## 結論

- **SafeUrl VO 適用**: `site_url`, `endpoint_url` (2フィールド)
- **CHECK 制約追加**: `name` の空文字列防止 (1フィールド)
- **変更不要**: `id`, `source_type`(既存CHECK), `is_active`, `created_at`, `updated_at`(DBトリガー)
- **副次的修正**: ルーターの冗長な `updated_at = datetime.now(UTC)` 削除を検討

## スコープ

### 今回（models 層 VO 見直し）

- モデルの `site_url` / `endpoint_url` を `Mapped[SafeUrl]` に変更
- `name` の CHECK 制約追加（Alembic マイグレーション）
- ルーター/フェッチャーの SafeUrl ↔ str 変換は暫定 `str()` + TODO コメント

### 後続（スキーマ層見直しフェーズ）

- `NewsSourceResponse.site_url` / `endpoint_url` を `str` → `SafeUrl` 対応に変更
- ルーターの暫定 `str()` 変換を解消
- フェッチャー層の `str()` 変換を解消
