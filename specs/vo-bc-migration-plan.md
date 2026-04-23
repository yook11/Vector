# 実装プラン: 値オブジェクトを BC 配下に再配置

> ステータス: 実装着手可
> 想定ブランチ: `refactor/vo-bc-migration`
> 前提 spec: 本ファイルで spec 兼プランを兼ねる

## 背景

現状 `app/domain/` にフラットに並んでいる値オブジェクト (VO) は、元々 `services/`/`repositories/`/`schemas/` の 3 層フラット構成に収まらないコア概念の受け皿として作られた。その後 `app/collection/` / `app/analysis/` / `app/search/` が自前の service/repository を持つ bounded context (BC) として自立したため、VO が属する BC に寄せるほうが凝集性が高まる。

実装例リサーチ（iktakahiro/dddpy、qu3vipon/python-ddd、kgrzybek/modular-monolith-with-ddd、VaughnVernon/IDDD_Samples 等）でも、「BC 配下に value objects を閉じる + 汎用プリミティブは shared kernel」が多数派パターン。

## スコープ

### 含む

- VO を BC 配下 `{bc}/domain/value_objects/` に物理移動
- 汎用プリミティブ `SafeUrl` を `app/shared/value_objects/` に新設して移動
- 既存 `from app.domain.*` import の全置換
- テストファイルの位置追従

### 含まない（別 spec）

- **pure domain ↔ ORM 分離**（アグリゲート本体の分離）。今回の移動は「VO の置き場」のみで、`models/` が VO を import する構図はそのまま残る
- `models/types.py` + `models/base.py` の横串 TypeDecorator ハブ解体
- `Category` / `NewsSource` / `Watchlist` の BC 化
- 機能追加・振る舞い変更は一切しない

`feedback_spec_scope_single_concern.md` に基づき、pure domain 分離は別 spec で BC パイロット（`analysis/classifier` の `Topic` 候補）として進める。

## 配置計画

| VO | From | To | 判定根拠 |
|---|---|---|---|
| `SafeUrl` | `app/domain/safe_url.py` | `app/shared/value_objects/safe_url.py` | 汎用プリミティブ、16 ファイル横断、BC を跨ぐ |
| `EntityName`, `EntityType` | `app/domain/entity.py` | `app/analysis/domain/value_objects/entity.py` | `ArticleEntity` アグリゲートの部品、analysis 内 4 ファイルで閉じる |
| `TopicName` | `app/domain/topic.py` | `app/analysis/domain/value_objects/topic.py` | `Topic` アグリゲートの部品、analysis/classifier の概念 |
| `SourceName` | `app/domain/news_source.py` | `app/collection/domain/value_objects/source.py` | `NewsSource` アグリゲート（collection BC）の部品 |
| `CategoryName`, `CategorySlug` | `app/domain/category.py` | **移動しない** | `Category` は参照データで BC として切り出していない |

### 命名規約

- ディレクトリ名: 複数形スネーク `value_objects/`（iktakahiro 方式）
- ファイル名: そのファイルが扱う概念をスネーク（`entity.py`, `topic.py`, `safe_url.py`, `source.py`）
- クラス名: 既存のまま変更なし

## 事前確認事項

| # | 確認事項 | 確定事項 |
|---|---|---|
| Q1 | `app/domain/__init__.py` 経由の再 export を使っている箇所 | 1 件のみ（`app/models/news_source.py` L11 `from app.domain import SafeUrl`）。**直接 import に書き換え**で統一 |
| Q2 | `models/types.py` + `models/base.py` が VO の TypeDecorator ハブ | **今回は触らない**。import パスのみ更新（shared/analysis/collection 配下への参照に差し替え）。横串ハブ化は別 spec で解体 |
| Q3 | `CategoryName` / `CategorySlug` の位置 | `app/domain/category.py` に**残置**。将来 Category を BC 化する spec と同時に動かす |
| Q4 | `app/domain/__init__.py` の存廃 | `category.py` が残るので**残置**。`CategoryName` / `CategorySlug` のみ再 export |
| Q5 | tests 配置 | `tests/test_domain/test_safe_url.py` → `tests/test_shared/test_safe_url.py`、`test_category_values.py` は domain 側に残置（VO は動かさないため）。`test_ai_analyzer.py` の import パスは追従更新のみで位置は動かさない |
| Q6 | ブランチ戦略 | `main` から `refactor/vo-bc-migration` を切る。単一 PR で出す（半端な中間状態は import エラーになるため分割不可） |

## 実装ステップ

### Step 1: ディレクトリ構造新設

新規作成する `__init__.py`:

```
app/shared/__init__.py                         # 新規、docstring のみ
app/shared/value_objects/__init__.py           # 新規、SafeUrl を re-export
app/analysis/domain/__init__.py                # 新規、docstring のみ
app/analysis/domain/value_objects/__init__.py  # 新規、EntityName/EntityType/TopicName を re-export
app/collection/domain/__init__.py              # 新規、docstring のみ
app/collection/domain/value_objects/__init__.py # 新規、SourceName を re-export
```

### Step 2: VO ファイル移動（`git mv`）

```bash
git mv backend/app/domain/safe_url.py    backend/app/shared/value_objects/safe_url.py
git mv backend/app/domain/entity.py      backend/app/analysis/domain/value_objects/entity.py
git mv backend/app/domain/topic.py       backend/app/analysis/domain/value_objects/topic.py
git mv backend/app/domain/news_source.py backend/app/collection/domain/value_objects/source.py
# category.py は移動しない
```

移動時に VO ファイルの中身は変更しない（冒頭 docstring のモジュール名参照程度の追従があれば最小限）。

### Step 3: `__init__.py` 再 export

各新設 `value_objects/__init__.py` は以下形式:

```python
# app/shared/value_objects/__init__.py
from app.shared.value_objects.safe_url import SafeUrl

__all__ = ["SafeUrl"]
```

```python
# app/analysis/domain/value_objects/__init__.py
from app.analysis.domain.value_objects.entity import EntityName, EntityType
from app.analysis.domain.value_objects.topic import TopicName

__all__ = ["EntityName", "EntityType", "TopicName"]
```

```python
# app/collection/domain/value_objects/__init__.py
from app.collection.domain.value_objects.source import SourceName

__all__ = ["SourceName"]
```

`app/domain/__init__.py` 更新後:

```python
from app.domain.category import CategoryName, CategorySlug

__all__ = ["CategoryName", "CategorySlug"]
```

### Step 4: import 参照の全置換

移動対象 VO の import を差し替える。置換 map:

| 旧 import | 新 import |
|---|---|
| `from app.domain.safe_url import SafeUrl` | `from app.shared.value_objects.safe_url import SafeUrl` |
| `from app.domain import SafeUrl` | `from app.shared.value_objects import SafeUrl`（`models/news_source.py` のみ） |
| `from app.domain.entity import EntityName, EntityType` | `from app.analysis.domain.value_objects.entity import EntityName, EntityType` |
| `from app.domain.topic import TopicName` | `from app.analysis.domain.value_objects.topic import TopicName` |
| `from app.domain.news_source import SourceName` | `from app.collection.domain.value_objects.source import SourceName` |

#### 更新ファイル一覧（`backend/` 配下）

**SafeUrl**:
- `app/collection/ingestion/service.py`
- `app/collection/ingestion/registry.py`
- `app/collection/ingestion/candidate.py`
- `app/collection/ingestion/repository.py`
- `app/collection/ingestion/fetchers/hacker_news.py`
- `app/collection/ingestion/fetchers/rss/base.py`
- `app/collection/extraction/extractor.py`
- `app/collection/extraction/candidate.py`
- `app/models/news_source.py`（再 export もここで直接 import に差し替え）
- `app/models/types.py`
- `app/models/base.py`
- `app/models/discovered_article.py`
- `app/schemas/news_source.py`
- `app/schemas/embeds.py`
- テスト各種（Explore 報告済み）

**EntityName / EntityType**:
- `app/analysis/classification_service.py`
- `app/analysis/extraction/schema.py`
- `app/models/types.py`
- `app/models/base.py`

**TopicName**:
- `app/analysis/classifier/schema.py`
- `app/analysis/repository.py`
- `app/models/topic.py`
- `app/models/types.py`
- `app/models/base.py`
- `app/schemas/embeds.py`

**SourceName**:
- `app/collection/ingestion/registry.py`
- `app/models/news_source.py`
- `app/models/types.py`
- `app/models/base.py`
- `app/schemas/news_source.py`
- `app/schemas/embeds.py`

### Step 5: テスト位置の追従

```bash
mkdir -p backend/tests/test_shared
git mv backend/tests/test_domain/test_safe_url.py backend/tests/test_shared/test_safe_url.py
# test_category_values.py は domain/ に残置
```

`tests/test_shared/__init__.py` は空で新設。test 本体の import パスを Step 4 の置換 map に従って更新。

### Step 6: 検証

```bash
# import 残骸チェック
rg "from app\.domain\.(safe_url|entity|topic|news_source)" backend/ && echo "残骸あり"
rg "from app\.domain import" backend/

# 静的検証
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/

# テスト
uv run pytest tests/ -x -q
```

- OpenAPI 生成不要（外部 API レスポンス形状は不変）
- 循環 import: 各 VO ファイルは pydantic のみ依存で app.* への依存なし → 循環は発生しない想定。念のため `python -c "import app.main"` で import 可能を確認

## リスクと切り分け

### 破壊的変更

- **外部 API**: なし
- **DB スキーマ**: なし
- **内部 import**: `from app.domain.{safe_url,entity,topic,news_source}` を直接使っているコード（リポジトリ内 49 箇所）は全て新パスへ追従必要

### 中途半端な状態が作れない

VO ファイルを移動した瞬間、旧 import は全て壊れる。Step 2〜4 はまとめて 1 コミットで行う（分割するとツリー全体が import エラーになる）。

### rollback

`git revert` 1 回で戻せる。data migration もないので完全に可逆。

## 変更/新規ファイル一覧

### 新規

- `backend/app/shared/__init__.py`
- `backend/app/shared/value_objects/__init__.py`
- `backend/app/analysis/domain/__init__.py`
- `backend/app/analysis/domain/value_objects/__init__.py`
- `backend/app/collection/domain/__init__.py`
- `backend/app/collection/domain/value_objects/__init__.py`
- `backend/tests/test_shared/__init__.py`

### 移動（`git mv` で履歴保持）

- `backend/app/domain/safe_url.py` → `backend/app/shared/value_objects/safe_url.py`
- `backend/app/domain/entity.py` → `backend/app/analysis/domain/value_objects/entity.py`
- `backend/app/domain/topic.py` → `backend/app/analysis/domain/value_objects/topic.py`
- `backend/app/domain/news_source.py` → `backend/app/collection/domain/value_objects/source.py`
- `backend/tests/test_domain/test_safe_url.py` → `backend/tests/test_shared/test_safe_url.py`

### 変更（import 追従のみ）

- `backend/app/domain/__init__.py`
- `backend/app/collection/ingestion/{service,registry,candidate,repository}.py`
- `backend/app/collection/ingestion/fetchers/hacker_news.py`
- `backend/app/collection/ingestion/fetchers/rss/base.py`
- `backend/app/collection/extraction/{extractor,candidate}.py`
- `backend/app/analysis/classification_service.py`
- `backend/app/analysis/repository.py`
- `backend/app/analysis/extraction/schema.py`
- `backend/app/analysis/classifier/schema.py`
- `backend/app/models/{types,base,news_source,discovered_article,topic,category}.py`
- `backend/app/schemas/{news_source,embeds,articles,category}.py`
- `backend/tests/test_domain/__init__.py`（もし存在し、移動 VO を import していれば）
- その他テスト（Explore 報告済みの 10 ファイル群）

## 次の spec への橋渡し

本 spec 完了後、次の spec で扱う:

1. **pure domain / ORM 分離パイロット**: `analysis/classifier` の `Topic` アグリゲートで Data Mapper を導入、実測
2. **横串 TypeDecorator ハブ解体**: `models/types.py` / `models/base.py` を各 BC の infrastructure 層に分散
3. **未整備 BC の切り出し**: `Category` を BC として独立させるか、参照データのまま据え置くかの判断

## 再開時のチェックリスト

1. 本プランを通読
2. `main` から `refactor/vo-bc-migration` を切る
3. Step 1〜5 を単一コミットで実施（ツリー全体を一貫した状態に保つ）
4. Step 6 検証コマンド全通過を確認
5. PR 作成
