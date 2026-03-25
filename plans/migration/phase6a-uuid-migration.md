# Phase 6a: Better Auth UUID 化 + バックエンド UUID 型統一

> 作成日: 2026-03-26
> ブランチ: feature/better-auth (既存)
> 前提: Phase 4 Step 4 (コード切替) 完了後に実施
> **スコープ: 開発環境限定。本番に既存ユーザーがいる場合はデータマイグレーションが別途必要。**

## 概要

Better Auth の ID 生成を cuid → UUID に変更し、`auth.user.id` を PostgreSQL `uuid` 型に統一する。
バックエンド側も `CurrentUser.id` を `str` → `uuid.UUID` に変更し、境界で1回パースする。

今後 `user_id` を持つテーブルが増えるたびに前提となる基盤変更。
既存ユーザー1名・watchlists データ0件の今が最もコストが低い。

### 方針

- **Better Auth UUID 変更**: `auth.ts` に `advanced.database.generateId: "uuid"` 追加
- **auth.user.id 型変更**: CLI migrate で `uuid` に変わらなければ、手動で FK DROP → 型変更 → FK 再作成
- **バックエンド UUID 型統一**: `CurrentUser.id` を `str` → `UUID` に変更し、境界で1回パース
- **auth.user 参照定義**: SQLAlchemy MetaData に `auth.user` の参照用 `Table` を登録（Phase 6b の FK 解決に必要）

---

## Step 1: Better Auth UUID 化 + auth.user.id 型変更

### 背景

Better Auth のデフォルトは cuid（`text` 型）。UUID v4 に変更し、`auth.user.id` を `uuid` 型に統一する。

- CLI migrate が `text` → `uuid` を自動で ALTER するかは不明（公式ドキュメントでは明記なし）
- CLI migrate が型変更しない場合は、手動で FK DROP → 型変更 → FK 再作成 を実行する

### フロントエンド影響確認（確認済み）

`session.user.id` は2箇所で `X-User-ID` ヘッダーにセットしている:
- `frontend/src/app/api/proxy/[...path]/route.ts:31`
- `frontend/src/lib/api-client.ts:42`

Better Auth の TypeScript 型では `session.user.id` は常に `string`。DB が `uuid` 型でも JS 側には文字列として渡る。HTTP ヘッダーも文字列。**フロントエンドの変更は `auth.ts` のみ。**

### 現状

- `auth.user.id` の型: `text`
- 既存ユーザー: 1名のみ
- `user_id` を持つ public テーブル: `watchlists` のみ（データ0件）

### 変更ファイル

**`frontend/src/lib/auth.ts`**:
```ts
export const auth = betterAuth({
  // ...existing config...
  advanced: {
    database: {
      generateId: "uuid",
    },
  },
});
```

### 実行手順

1. `auth.ts` に `advanced.database.generateId: "uuid"` を追加
2. 既存の auth データをクリア（FK 依存順序に注意）:
   ```sql
   DELETE FROM auth.session;
   DELETE FROM auth.account;
   DELETE FROM auth."user";
   ```
3. Better Auth CLI migrate を **`--yes` なしで** 実行し、提案される変更を確認:
   ```bash
   cd frontend && npx @better-auth/cli@latest migrate
   ```
4. **確認ポイント（適用前）**:
   - `id` カラムの型変更（`text` → `uuid`）が提案されるか？
   - session / account / verification の `userId` FK も連動して変わるか？
   - 問題なければ適用を承認
5. 適用後に `auth.user.id` の型を確認:
   ```sql
   \d auth."user"
   ```
6. **CLI migrate が `uuid` に変更しなかった場合**、手動で FK DROP → 型変更 → FK 再作成:
   ```sql
   -- 6a. FK を DROP（子テーブルから）
   ALTER TABLE auth.session DROP CONSTRAINT IF EXISTS "session_userId_fkey";
   ALTER TABLE auth.account DROP CONSTRAINT IF EXISTS "account_userId_fkey";

   -- 6b. 全テーブルの id/userId カラムを uuid 型に変更
   ALTER TABLE auth."user" ALTER COLUMN id TYPE uuid USING id::uuid;
   ALTER TABLE auth.session ALTER COLUMN id TYPE uuid USING id::uuid;
   ALTER TABLE auth.session ALTER COLUMN "userId" TYPE uuid USING "userId"::uuid;
   ALTER TABLE auth.account ALTER COLUMN id TYPE uuid USING id::uuid;
   ALTER TABLE auth.account ALTER COLUMN "userId" TYPE uuid USING "userId"::uuid;
   ALTER TABLE auth.verification ALTER COLUMN id TYPE uuid USING id::uuid;

   -- 6c. FK を再作成
   ALTER TABLE auth.session
     ADD CONSTRAINT "session_userId_fkey"
     FOREIGN KEY ("userId") REFERENCES auth."user"(id) ON DELETE CASCADE;
   ALTER TABLE auth.account
     ADD CONSTRAINT "account_userId_fkey"
     FOREIGN KEY ("userId") REFERENCES auth."user"(id) ON DELETE CASCADE;
   ```
7. 再サインアップで UUID が生成されることを確認

### 確認結果（実施時に記入）

| チェック項目 | 結果 |
|-------------|------|
| CLI migrate が型変更したか | _（実施後に記入）_ |
| 手動 ALTER が必要だったか | _（実施後に記入）_ |
| `auth.user.id` の最終的な型 | `uuid`（確定） |

---

## Step 2: バックエンド UUID 型統一

### 背景

`auth.user.id` が `uuid` 型になるため、バックエンド側も `str` ではなく `uuid.UUID` 型で統一する。
境界（BFF ヘッダー受信）で1回パースし、以降は型安全に扱う。

### 変更ファイル

#### `backend/app/dependencies.py` — 境界でのパース

```python
from uuid import UUID

@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: UUID  # str → UUID
    role: str

async def get_current_user(request: Request) -> CurrentUser:
    # ...existing validation...
    user_id = request.headers.get("X-User-ID")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        parsed_id = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid user ID format")

    role = request.headers.get("X-User-Role", "user")
    return CurrentUser(id=parsed_id, role=role)


async def get_optional_user(request: Request) -> CurrentUser | None:
    """Like get_current_user but returns None instead of raising 401.

    ただし Secret が正しく X-User-ID が存在するのに UUID として不正な場合は
    BFF のバグなので 401 を返す（バグを握りつぶさない）。
    """
    secret = request.headers.get("X-Internal-Secret")
    if secret != settings.internal_api_secret:
        return None  # 未認証 → OK

    user_id = request.headers.get("X-User-ID")
    if not user_id:
        return None  # ヘッダーなし → OK

    # ここに到達 = BFF が認証済みユーザーとして送ってきた
    # UUID が不正ならシステムバグなので 401
    try:
        parsed_id = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid user ID format")

    role = request.headers.get("X-User-Role", "user")
    return CurrentUser(id=parsed_id, role=role)
```

#### `backend/tests/conftest.py` — テスト用 ID を有効な UUID に変更

```python
TEST_USER_ID = "00000000-0000-4000-a000-000000000001"
TEST_ADMIN_ID = "00000000-0000-4000-a000-000000000002"
```

### 影響しないファイル（変更不要）

| ファイル | 理由 |
|---------|------|
| `routers/me.py` | `user.id` をそのまま使用。SQLAlchemy が `UUID` 型と DB カラムの比較を処理 |
| `routers/news.py` | 同上 |
| `routers/keywords.py` | `_user: CurrentUser` は認可チェックのみ。`user.id` を参照しない |
| `routers/news_sources.py` | 同上 |

---

## Step 3: auth.user 参照テーブル定義

Better Auth の `auth.user` は SQLModel の MetaData に登録されていないため、Phase 6b で `watchlist_entries.user_id` → `auth.user.id` の FK を定義する際に FK 解決に失敗する。

SQLAlchemy が FK 文字列 `"auth.user.id"` を解決するためのメタデータ登録を行う。実テーブルを作成するわけではない。

#### リサーチで確定した構文ルール

| 項目 | 結論 |
|------|------|
| `ForeignKey("auth.user.id")` | 動く。`schema.table.column` の3部構成は公式サポート |
| 文字列内の `user` クォート | **してはいけない**。SQLAlchemy が DDL 生成時に自動クォート（`REFERENCES auth."user"(id)`） |
| `sa_column` パターン | 動く。ForeignKey の解決は SQLAlchemy コアレイヤーで処理される |
| 参照先 MetaData 登録 | **必要**。`auth.user` が SQLModel MetaData にないと FK 解決に失敗する |

### 変更ファイル

**`backend/app/models/auth_ref.py`**（新規作成）:
```python
"""auth スキーマのテーブル参照定義（FK 解決用）。

Better Auth が管理する auth.user テーブルを SQLAlchemy MetaData に登録する。
実テーブルの作成・管理は Better Auth CLI が行うため、ここでは参照のみ。
"""
from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlmodel import SQLModel

auth_user_ref = Table(
    "user",
    SQLModel.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
    schema="auth",
)
```

**`backend/app/models/__init__.py`**:
- `auth_ref` をインポートに追加（MetaData 登録をトリガーするため）

---

## Step 4: 検証

### 検証プロトコル

```bash
# Backend
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend
cd frontend && npx biome check src/ && npx tsc --noEmit
```

---

## 影響ファイルまとめ

| ファイル | Step | 変更種別 |
|---------|------|---------|
| `frontend/src/lib/auth.ts` | 1 | 変更 (generateId 追加) |
| `backend/app/dependencies.py` | 2 | 変更 (CurrentUser.id → UUID, パース追加) |
| `backend/tests/conftest.py` | 2 | 変更 (TEST_USER_ID/TEST_ADMIN_ID を有効な UUID に) |
| `backend/app/models/auth_ref.py` | 3 | 新規作成 (auth.user 参照定義) |
| `backend/app/models/__init__.py` | 3 | 変更 (auth_ref インポート追加) |

## リスク

| リスク | 対策 |
|-------|------|
| CLI migrate が `text` → `uuid` を自動変更しない | 手動 FK DROP → ALTER → FK 再作成の SQL を Step 1 に用意済み。auth データクリア後なので安全 |
| 手動 ALTER で FK 制約の型不整合エラー | FK を先に DROP してから型変更し、最後に FK を再作成する順序で回避 |
| `CurrentUser.id` の型変更でルーターが壊れる | SQLAlchemy が `UUID` 型と DB カラムの比較を自動処理するため、ルーター側のコード変更は不要。テストで検証 |
| テストの `TEST_USER_ID` 変更で既存テストが失敗 | UUID 形式に変更するだけで、テストロジック自体は変わらない。全テスト実行して検証 |
| `auth_user_ref` の MetaData 登録で Alembic autogenerate が auth テーブルを検出 | `alembic/env.py` の `include_name` で `auth` スキーマを除外済み。autogenerate には影響しない |
