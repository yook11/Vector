# Phase 6b: watchlists → watchlist_entries リファクタリング

> 作成日: 2026-03-26
> ブランチ: feature/better-auth (既存)
> スペック: `specs/db-redesign.md/watchlist-entries-table.md`
> 前提: **Phase 6a (UUID 化) 完了後**に実施
> **スコープ: 開発環境限定。**

## 概要

`watchlists` テーブルを `watchlist_entries` にリネームし、サロゲートキー (`id`) を廃止して複合PK `(user_id, news_article_id)` に変更する。`user_id` に `auth.user(id)` への FK を追加。

### 方針

- **watchlists データ0件**: DROP → CREATE が最もクリーン
- モデルクラス名: `WatchlistItem` → `WatchlistEntry` にリネーム
- Phase 6a で `auth.user.id` が `uuid` 型に変更済み → `user_id` も `UUID` 型で定義

### ロールバック戦略

| 状態 | アプリ動作 |
|------|-----------|
| Step 1 完了、Step 2 未実施 | モデルが旧テーブル名 `watchlists` を参照するためエラー |

→ Step 1 〜 Step 4 は**一連の作業として連続実施**し、中間状態でのデプロイは行わない。

---

## Step 1: Alembic マイグレーション

### 事前チェック（手動SQL）

| チェック項目 | 確認コマンド |
|-------------|------------|
| `watchlists` のデータ件数 | `SELECT count(*) FROM watchlists;` |
| `auth.user.id` の型 | `\d auth."user"` → `uuid` であること |
| `watchlists` を FK 参照しているテーブル | 下記クエリ参照 |
| 現在の Alembic head | `SELECT version_num FROM alembic_version;` |

```sql
-- PostgreSQL で特定テーブルを参照する FK を確認
SELECT
    tc.table_schema, tc.table_name, tc.constraint_name,
    ccu.table_schema AS ref_schema, ccu.table_name AS ref_table
FROM information_schema.table_constraints tc
JOIN information_schema.constraint_column_usage ccu
    ON tc.constraint_name = ccu.constraint_name
    AND tc.table_schema = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
    AND ccu.table_name = 'watchlists';
```

### 事前チェック結果（Phase 6a 前の値）

| チェック項目 | 結果 |
|-------------|------|
| `watchlists` のデータ件数 | **0件** |
| `watchlists` を FK 参照しているテーブル | **なし** |
| 現在の Alembic head | `c6b1a2b3c4d5` |

### マイグレーション

**手書きマイグレーション 1本**（DDL のみ、データ移行なし）

`auth` スキーマは Alembic の autogenerate 対象外（`env.py` の `include_name` で除外済み）のため、cross-schema FK を含むマイグレーションは **autogenerate ではなく手書き** で作成する。

#### upgrade

1. `watchlists` テーブルを DROP（関連インデックス・制約・シーケンスも自動削除）
2. `watchlist_entries` テーブルを CREATE:

```sql
CREATE TABLE watchlist_entries (
    user_id         UUID                     NOT NULL,
    news_article_id INTEGER                  NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    PRIMARY KEY (user_id, news_article_id),
    FOREIGN KEY (user_id)         REFERENCES auth."user"(id) ON DELETE CASCADE,
    FOREIGN KEY (news_article_id) REFERENCES news_articles(id) ON DELETE CASCADE
);
```

Alembic の `op.create_table` で cross-schema FK を定義する方法:
- `sa.ForeignKeyConstraint(["user_id"], ["auth.user.id"], ondelete="CASCADE")` — 3部構成で指定。文字列内の `user` はクォート不要（Alembic/SQLAlchemy が DDL 生成時に自動クォート）
- または `op.execute()` で上記の生 SQL を直接実行

#### downgrade

1. `watchlist_entries` テーブルを DROP
2. `watchlists` テーブルを再作成（元の構造: `id` serial PK、`user_id` VARCHAR(32)、UNIQUE制約、**auth.user への FK なし**）

> **注記**: downgrade で再作成する `watchlists` テーブルには元々 `auth.user` への FK がなかったため、`user_id` は `VARCHAR(32)` のまま FK なしで再作成する。Phase 6a で `auth.user.id` は `uuid` に変更済みだが、FK がないため型不一致は問題にならない。

---

## Step 2: モデル変更

**対象ファイル:** `backend/app/models/watchlist.py`

| 変更項目 | 現行 | 新 |
|---------|------|-----|
| クラス名 | `WatchlistItem` | `WatchlistEntry` |
| `__tablename__` | `watchlists` | `watchlist_entries` |
| `id` フィールド | `int \| None, primary_key=True` | **削除** |
| `user_id` | `String(32), index=True` | `PgUUID(as_uuid=True), primary_key=True`, FK `auth.user.id CASCADE` |
| `news_article_id` | `Integer, FK news_articles(id) CASCADE` | `Integer, primary_key=True`, FK `news_articles(id) CASCADE` |
| `__table_args__` | `UniqueConstraint(...)` | 不要（複合PKに吸収） |
| `created_at` | `default_factory=lambda: datetime.now(UTC)` | `server_default=func.now()` |
| Relationship | `back_populates="watchlist_items"` | `back_populates="watchlist_entries"` |

### コード例

```python
import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlmodel import Field, Relationship, SQLModel


class WatchlistEntry(SQLModel, table=True):
    __tablename__ = "watchlist_entries"

    user_id: uuid_mod.UUID = Field(
        sa_column=Column(
            PgUUID(as_uuid=True),
            ForeignKey("auth.user.id", ondelete="CASCADE"),  # クォート不要、自動クォートされる
            primary_key=True,
        )
    )
    news_article_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("news_articles.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        )
    )

    # Relationships
    news_article: "NewsArticle" = Relationship(back_populates="watchlist_entries")
```

> FK 解決に必要な `auth.user` の参照テーブル定義は Phase 6a Step 3 で `backend/app/models/auth_ref.py` に作成済み。

#### `server_default=func.now()` への変更について

テストで `WatchlistItem` を直接インスタンス化して `created_at` を明示セットしている箇所はない（テストは HTTP リクエスト経由）。`server_default` への変更による既存テストへの影響はなし。

**連動変更:**
- `backend/app/models/news.py` — `watchlist_items` → `watchlist_entries` (back_populates 側)
- `backend/app/models/__init__.py` — `WatchlistItem` → `WatchlistEntry` エクスポート

---

## Step 3: スキーマ + ルーター変更

### スキーマ変更

**対象ファイル:** `backend/app/schemas/user.py`

| 変更項目 | 現行 | 新 |
|---------|------|-----|
| `WatchlistResponse.id` | `int` | **削除** |

`WatchlistCreate`, `WatchlistListResponse` は変更なし。

> **Breaking Change**: `WatchlistResponse` から `id` フィールドを削除するため、OpenAPI スキーマが変わる。フロントエンドでは `id` を使用していないことを確認済み。外部からのAPI利用もないポートフォリオプロジェクトのため、バージョニングは不要と判断。

### ルーター変更

**対象ファイル:** `backend/app/routers/me.py`

| 箇所 | 変更内容 |
|------|---------|
| import | `WatchlistItem` → `WatchlistEntry` |
| `list_watchlist` (L50) | `id=item.id` を削除 |
| `add_to_watchlist` (L103) | `id=item.id` を削除 |
| `WatchlistItem` 参照 (全箇所) | `WatchlistEntry` にリネーム |

**対象ファイル:** `backend/app/routers/news.py`

| 箇所 | 変更内容 |
|------|---------|
| import | `WatchlistItem` → `WatchlistEntry` |
| `_get_watched_ids()` 内 | `WatchlistItem` → `WatchlistEntry` (ロジック変更なし) |

---

## Step 4: テスト修正 + 検証

**対象ファイル:** `backend/tests/test_routers/test_me.py`

- `WatchlistItem` → `WatchlistEntry` のインポート修正（使用している場合）
- レスポンスに `id` が含まれなくなるためアサーション修正（該当箇所があれば）
- テスト内の `conftest.py` で WatchlistItem を使っている場合はリネーム

### 検証プロトコル

```bash
# Backend
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend
cd frontend && npx biome check src/ && npx tsc --noEmit
```

---

## Step 5: フロントエンド型再生成

```bash
cd frontend && npm run generate-types
```

- `WatchlistResponse` から `id` フィールドが消えるが、フロントエンド側で `id` を参照している箇所はない
- コンポーネント・API Client の変更は不要

---

## 影響ファイルまとめ

| ファイル | Step | 変更種別 |
|---------|------|---------|
| `backend/alembic/versions/c7_*.py` | 1 | 新規作成（手書き） |
| `backend/app/models/watchlist.py` | 2 | 変更 |
| `backend/app/models/news.py` | 2 | 変更 (back_populates) |
| `backend/app/models/__init__.py` | 2 | 変更 (エクスポート) |
| `backend/app/schemas/user.py` | 3 | 変更 |
| `backend/app/routers/me.py` | 3 | 変更 |
| `backend/app/routers/news.py` | 3 | 変更 |
| `backend/tests/test_routers/test_me.py` | 4 | 変更 |
| `frontend/src/types/generated.ts` | 5 | 自動再生成 |

## リスク

| リスク | 対策 |
|-------|------|
| Phase 4 残作業との競合 (`me.py`) | Phase 4 完了後に Phase 6b を実施する前提 |
| cross-schema FK の SQLAlchemy 構文 | リサーチ確認済み: `ForeignKey("auth.user.id")` が正しい構文。クォート不要。参照用 `Table` 定義は Phase 6a で MetaData に登録済み |
| Step 1 完了後 Step 2 前の中間状態でアプリが壊れる | Step 1 〜 Step 4 を一連の作業として連続実施。中間状態でのデプロイは行わない |
