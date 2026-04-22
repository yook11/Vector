# Collection 層のエラー型共通化

> ステータス: 設計確定（実装待ち）
> ブランチ: `refactor/collection-errors-common`

## 目的

`PermanentFetchError` / `TemporaryFetchError` の定義場所を `app/collection/extraction/extractor.py` から `app/collection/errors.py` に移動し、collection 配下（ingestion / extraction）で共有できる状態にする。

本 PR ではロジックの変更は行わない。**定義の移動と import パスの書き換えのみ**。

## 解決する問題

現状、`PermanentFetchError` / `TemporaryFetchError` は `extraction/extractor.py` に定義されている。意味的には「外部リソース取得の Permanent / Temporary 失敗」を表すドメイン例外で、extraction だけでなく ingestion でも同じ判断軸（HTTP status + ネットワークエラー）で raise されるべきもの。

後続の refactor（`refactor/collection-ingestion-errors`）で ingestion 側 Fetcher を例外化する際、定義位置が `extraction` 配下のままだと:

- `from app.collection.extraction.extractor import TemporaryFetchError` を ingestion 側から import することになる
- extraction と ingestion の間に不自然な依存方向が生まれる
- 「Fetch 失敗」という共通概念の所属が一方のレイヤーに偏る

これを避けるため、先に中立な場所に定義を移す。

## 設計判断

### 命名: 既存の `PermanentFetchError` / `TemporaryFetchError` を維持

- 既存コード（extraction 側 + テスト）への影響を最小化
- "fetch" の語は HTTP 取得だけでなく「外部リソースの取り寄せ」全般に使える語感で、ingestion の feed 取得にも馴染む
- 将来 ingestion 側で feedparser bozo を Permanent として raise する場合も意味的に妥当

### 配置: `app/collection/errors.py`

- collection 配下（ingestion / extraction）で共有される概念
- `app/analysis/errors.py` と対称構造
- 将来、collection 固有の他のドメイン例外が増える場合の拡張余地

### スコープ: Phase 1 のみ

本 PR では **定義の移動と import 書き換え以外は一切やらない**:

- ingestion 側の例外化は行わない（別 PR: Phase 2）
- `SourceFetchResult` の整理は行わない（別 PR: Phase 2）
- 名称変更、継承関係の変更、メソッド追加などは行わない

理由:
- diff を最小化してレビュー負荷を下げる
- ロジック変更ゼロを保証することで safe revert 可能な単位にする
- Phase 2 以降の基盤になる

## 変更ファイル

### 新規作成

- `backend/app/collection/errors.py`
  - `PermanentFetchError` / `TemporaryFetchError` の定義
  - docstring は現行を継承

### 修正（定義の移動元）

- `backend/app/collection/extraction/extractor.py`
  - class 定義を削除
  - `from app.collection.errors import PermanentFetchError, TemporaryFetchError` を冒頭に追加

### 修正（import パス変更のみ）

- `backend/app/collection/extraction/service.py:20`
  - `from app.collection.extraction.extractor import PermanentFetchError`
  - → `from app.collection.errors import PermanentFetchError`

- `backend/app/collection/tasks.py:22-25`
  - `from app.collection.extraction.extractor import (ArticleHtmlExtractor, TemporaryFetchError)`
  - → `ArticleHtmlExtractor` は現状のパスから import、`TemporaryFetchError` は `app.collection.errors` から import

- `backend/tests/test_html_extractor.py:12-13`
  - import パス変更

- `backend/tests/test_content_service.py:13-14`
  - import パス変更

- `backend/tests/test_content_tasks.py:7`
  - import パス変更

## 手順

### Step 1: 新ファイル作成

`backend/app/collection/errors.py` を作成し、`PermanentFetchError` / `TemporaryFetchError` を定義する。docstring は現行と同一。

### Step 2: extraction 側の定義を移動

`backend/app/collection/extraction/extractor.py` から class 定義を削除し、`from app.collection.errors import PermanentFetchError, TemporaryFetchError` に置き換える。

この2ステップは同一コミットで行う必要がある（extraction から定義を消した瞬間、`service.py` や tests が import エラーになるため、`errors.py` が先に存在している必要がある）。

### Step 3: 既存 import パスの書き換え

以下を `from app.collection.errors import ...` に書き換える:

- `app/collection/extraction/service.py`
- `app/collection/tasks.py`
- `tests/test_html_extractor.py`
- `tests/test_content_service.py`
- `tests/test_content_tasks.py`

`ArticleHtmlExtractor` 等の同じモジュールから import している他のシンボルは現状のパスを維持する（`tasks.py:22-25` は2つの import 文に分かれる）。

### Step 4: 検証

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/ -x -q
```

すべて緑であること。ロジックは変更していないので、全 294 テストがそのまま通るはず。

### Step 5: コミット & PR

- コミットメッセージ: `refactor(collection): extract fetch errors to collection/errors.py`
- PR base: `main`
- `/review` スキル実行

## 成立する不変条件

- `PermanentFetchError` / `TemporaryFetchError` は `app/collection/errors.py` のみで定義される（単一の source of truth）
- 他のモジュールは同じ型を共有する（isinstance チェックが全体で一貫する）
- ロジックは変更していないので、既存テストはそのまま通る

## 後続 Phase

この Phase 完了後、以下の refactor が続く想定:

- **Phase 2** (`refactor/collection-ingestion-errors`): ingestion 側の Fetcher を例外化、`SourceFetchResult` から `success` / `error_message` / `etag` / `last_modified` / `not_modified` / `source_id` を削除
- **Phase 3** (`refactor/ingestion-article-candidate`): `ArticleCandidate` を VO 化（`Title` VO 新設）、`description` / `content` / `published_at` のデッドフィールド削除
- **Phase 4**: `BaseRssFetcher.fetch` / `HackerNewsFetcher.fetch` の内部責務分離（HTTP / Parse / Convert / Persist）

各 Phase は独立して PR 化される。

## リスク

- **低**: 定義の移動と import 書き換えのみで、実行時挙動は変わらない
- テストが全 pass することを確認できれば、regression の可能性はほぼない
- 例外クラスの identity は `class Foo(Exception)` で Python 側に生成されるので、import パスが変わっても `isinstance` は一貫する
