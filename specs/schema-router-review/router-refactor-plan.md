# ルーター層リファクタリング計画

## 背景

スキーマ層リファクタリング（命名規約統一・VO 適用）が Category / Keyword / News / NewsSource で完了。
ルーター層のレビューを進める中で、全ルーターに共通する構造的問題が判明した。

## 問題

現状のルーターは「太ったルーター」になっている:

- DB クエリの組み立て・実行
- データの変換・集約
- Pydantic レスポンスの組み立て
- HTTP パラメータ処理・レスポンス返却

これらが単一のエンドポイント関数に集結しており、関数を開いたときに SQL クエリ構文が最初に目に入り、**何をしているか**を掴むにはクエリの中身まで読む必要がある。

## 方針: 3 層分離

| レイヤー | 責務 | 配置 | 返却型 |
|---|---|---|---|
| **Repository** | DB クエリの組み立て・実行 | `app/repositories/<domain>.py` | Row / Model |
| **Service** | データの変換・集約・組み立て | `app/services/<domain>.py` | Pydantic スキーマ |
| **Router** | HTTP 関連（Depends, パラメータ, レスポンス返却） | `app/routers/<domain>.py` | Service の戻り値をそのまま |

### 設計判断

- **Service は Pydantic スキーマを直接返す**。中間 DTO は作らない（プロジェクト規模に対して過剰）
- Repository は SQLAlchemy の Row やモデルインスタンスを返す
- Router は可能な限り薄く。ビジネスロジック・データ変換を一切持たない

## レビュー・実装順序

依存関係と複雑さを考慮し、以下の順序で進める。

### Phase 1: パターン確立（単純なルーターで型を作る）

#### 1. categories（87行 / 1 エンドポイント）

- **レビュー結果**: スキーマ・VO は問題なし。BLACKLISTED キーワードのフィルタ未実装だが、現時点では全件 OFFICIAL のため対応不要（AI 自動キーワード検出の実装時に対応）
- **実装**: Repository + Service に分離し、3 層パターンのリファレンス実装とする

| 新規ファイル | 内容 |
|---|---|
| `app/repositories/category.py` | カテゴリ・キーワード・記事数の 3 クエリ |
| `app/services/category.py` | 集約ロジック（kw_by_cat グルーピング等）→ `CategoryDetailList` 返却 |

#### 2. news_sources（131行 / 5 エンドポイント）

- **レビュー**: 未実施
- **注目点**: `_to_response()` ヘルパーが既に存在。CRUD パターンの分離例になる

#### 3. keywords（169行 / 4 エンドポイント）

- **レビュー**: 未実施
- **注目点**: CategoryEmbed の組み立て、article count の集計

### Phase 2: 中核ルーターの分離

#### 4. news（358行 / 5 エンドポイント）

- **レビュー**: 未実施
- **注目点**: 最大・最重要。ヘルパー関数 4 つ（`_build_news_brief`, `_build_news_detail`, `_build_keyword_embeds`, `_news_eager_options`）を Service / Repository に配置。Watchlist と共有する基盤になる
- **既存計画との関係**: watchlist.md で計画していた `_news_helpers.py` の抽出は、Service / Repository 層への配置に置き換わる

| 移動先 | 関数 |
|---|---|
| `app/repositories/news.py` | `_news_eager_options()`, クエリ構築, `_get_watched_ids()` |
| `app/services/news.py` | `_build_news_brief()`, `_build_news_detail()`, `_build_keyword_embeds()` |

#### 5. me（150行 / 3 エンドポイント — Watchlist）

- **レビュー**: 未実施
- **注目点**: watchlist.md の再設計（`WatchlistResponse` 廃止 → `PaginatedNewsResponse` 統一、POST 201 空ボディ化）を 3 層分離と同時に実施
- **依存**: news の Repository / Service を共有するため、news の分離完了後に着手

| 新規ファイル | 内容 |
|---|---|
| `app/repositories/watchlist.py` | watchlist エントリの CRUD クエリ |
| `app/services/watchlist.py` | news Service を利用して `PaginatedNewsResponse` を構築 |

## ディレクトリ構成（実装後）

```
backend/app/
├── repositories/
│   ├── __init__.py
│   ├── category.py
│   ├── keyword.py
│   ├── news.py
│   ├── news_source.py
│   └── watchlist.py
├── services/
│   ├── __init__.py        # ※ 既存の news_fetcher.py 等と共存
│   ├── category.py
│   ├── keyword.py
│   ├── news.py
│   ├── news_source.py
│   └── watchlist.py
├── routers/
│   ├── categories.py      # 薄い HTTP 層のみ
│   ├── keywords.py
│   ├── news.py
│   ├── news_sources.py
│   └── me.py
```

## 既存 spec との関係

| spec ファイル | 状態 | 本計画との関係 |
|---|---|---|
| `category.md` | スキーマ層レビュー完了 | Phase 1-1 で 3 層分離を実施 |
| `news_source.md` | スキーマ層レビュー完了 | Phase 1-2 で 3 層分離を実施 |
| `news.md` | スキーマ層レビュー完了 | Phase 2-4 で 3 層分離を実施 |
| `watchlist.md` | 再設計プラン策定済み | Phase 2-5 で 3 層分離と同時に再設計を実施。`_news_helpers.py` の計画は Service/Repository 層に置き換え |

## 検証

各ルーターの分離後に実施:

```bash
uv run ruff check app/
uv run ruff format --check app/
uv run pytest tests/ -x -q
```

フロントエンドへの影響がある場合（Watchlist）:

```bash
npm run generate-types
npx biome check src/
npx tsc --noEmit
```
