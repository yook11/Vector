# Admin Router Restructure — `/api/v1/admin/*`

> **Status**: Implemented (2026-04-10). Branch `refactor/extract-admin-router`.
> **Related (prior work)**: [`specs/backlog/news-source-list-role-split.md`](backlog/news-source-list-role-split.md) — ソースフィルタ削除+`/sources` admin 限定締め (完了済、PR #31)

---

## 前提と課題

Vector は **AWS デプロイ前提**の個人プロジェクト。現在 admin 専用エンドポイントが 2 つのルーターに分散しており、以下の課題がある:

1. **認可コードの散在**: 6 endpoint すべてで `_user: Annotated[CurrentUser, Depends(get_admin_user)]` を個別に書いており、書き忘れリスクがある
2. **ネットワーク層防御の未整備**: 将来 AWS WAF でパスベースの IP allowlist / rate limit ルールを書きたくなった時、`/api/v1/sources` と `/api/v1/pipeline` はそれぞれ別パスで、管理系 API を一括で識別できない
3. **スケーラビリティ**: 近い将来 AI 自動キーワード検出機能 (`project_keyword_status_filter.md` 参照) で新しい admin 専用ルーター (`keywords`) が追加される予定であり、現状のフラット配置はそのタイミングで必ず再編が必要になる
4. **不要コードの累積**: `NewsSourceDetailList.total` フィールドと `NewsSourceRepository.get_count()` は誰からも使われていない純粋なデッドコード (tests の `data["total"]` アサーション以外)
5. **Service 層命名の冗長**: `NewsSourceService.list_sources()` はクラスコンテキスト内で `sources` が重複しており、かつ `list` 動詞が「一覧を取得する」意図を弱く表現している

## 決定

admin 専用エンドポイントを **`backend/app/routers/admin/`** パッケージに集約し、URL prefix **`/api/v1/admin/*`** に統一する。router-level の認可依存で個別 endpoint の `_user` 引数を撤去する。同時にデッドコード削除と Service メソッドリネームを行う。

### 業界リサーチで確認した事実

- Shopify, GitHub, Microsoft Graph, Google はいずれも `/admin` プレフィックスを使用。`/manage`, `/internal` を URL prefix に使う主要プロバイダは存在しない
- FastAPI 公式チュートリアル "Bigger Applications - Multiple Files" が `include_router(prefix="/admin", dependencies=[...])` パターンを明示的に推奨
- 「`/admin` の硬直性」問題 (将来 editor ロール等で URL が嘘をつく) は理論的な懸念で、主要 API プロバイダでの実例記録はない
- FastAPI `routing.py` の 1421-1425 行で `include_router(dependencies=...)` が子ルートの `dependencies` にマージされる挙動を source レベルで確認済み

### 認可パターンの使い分けガイドライン (新規)

- **完全 admin ルーター** (例: `sources`, `pipeline`, 将来の `keywords`) → **router-level** `dependencies=[Depends(get_admin_user)]` で集約
- **admin / user 混在ルーター** (現状なし。将来発生した場合) → **endpoint-level** で個別に `Depends(get_admin_user)` を付与

## スコープ

### 含まれる

- `backend/app/routers/news_sources.py` と `backend/app/routers/pipeline.py` の両方を `backend/app/routers/admin/` 配下に移動
- URL 変更: `/api/v1/sources` → `/api/v1/admin/sources`, `/api/v1/pipeline/*` → `/api/v1/admin/pipeline/*`
- Router-level `dependencies=[Depends(get_admin_user)]` 導入 (このプロジェクトで初の router-level auth pattern)
- `NewsSourceService.list_sources()` → `get_all()` にリネーム
- `total` フィールド / `NewsSourceRepository.get_count()` / count 計算の完全削除
- `pipeline.py` の `Depends()` 旧スタイルを `Annotated` スタイルに統一 (既存コードベース規約に合わせる)
- フロント API client パス更新 (2 ファイル、6 箇所)
- 型再生成 (`/gen-types` スキル)
- 本 spec のステータス更新または移動
- 前身 `news-source-list-role-split.md` に status banner 追加

### 含まれない

- AI 自動キーワード検出 (`keywords` 新機能) — 別 PR
- `routers/admin/` ディレクトリを流用する他の将来機能 — 別 PR
- テストファイル自体のディレクトリ移動 (`test_admin/` サブディレクトリ化しない)
- `specs/endpoint-review/README.md` のエンドポイント一覧更新 — 別 PR
- `docs/04_API_SPECIFICATION.md` の更新 (存在すれば) — 別 PR

---

## 探索済みの事実 (実装前検証)

### Backend (src-level 検証済み)
- `backend/app/main.py` は `app.include_router(news_sources.router)` 等で登録、global prefix なし
- 各ルーターは自前で `/api/v1/...` 完全プレフィックスを定義
- `backend/app/dependencies.py:55-64` に `get_admin_user` 定義 — `get_current_user` (L21-26 の `CurrentUser` dataclass 返却) に依存
- **Router-level `dependencies=[...]` はこのプロジェクトで現状未使用** — 新パターン導入
- `NewsSourceRepository.get_count()` は **`service.list_sources()` 内でのみ呼ばれる** — 完全孤立、削除安全
- `NewsSourceService.list_sources()` は `router` 内でのみ呼ばれる — 呼び出し元は1箇所
- 各 router handler 内で `_user.` や `current_user.` を参照している箇所は **ゼロ** (Grep 検証済) — `_user` パラメータ削除は安全

### Frontend (src-level 検証済み)
- `sourcesData.total` へのアクセスはフロント全体で **ゼロ** — `.items` のみ使用。`total` 削除は安全
- `/sources` と `/pipeline/fetch` のハードコードは **`api-client.ts` と `client-api.ts` の 2 ファイルのみ** に限定
- BFF proxy (`frontend/src/app/api/proxy/[...path]/route.ts`) は **path-agnostic** — 新パスで透過的に動作、proxy 側の修正不要
- 消費側コンポーネント (settings/page.tsx, SourceManager.tsx, SourceTable.tsx, SourceFormDialog.tsx) はパスをハードコードせず関数呼び出しのみ — 追加修正不要

### Other (検証済)
- docker-compose, env example, shell scripts, CI 設定に `/api/v1/sources` / `/api/v1/pipeline` の参照なし
- `specs/schema-router-review/*`, `specs/endpoint-review/README.md`, `plans/archived/*` に historical 参照あり — 今回は触らない (historical として正しい)

### FastAPI 依存伝搬の挙動確認 (source レベル)
- `fastapi/routing.py` L1055-1057: router-level `self.dependencies` が `add_api_route` 時に各ルートへコピー
- `fastapi/routing.py` L1421-1425: `include_router(dependencies=...)` が親の deps を各子ルートの deps に **prepend**
- 結論: `admin_router(dependencies=[Depends(get_admin_user)]).include_router(news_sources.router)` で `get_admin_user` が全 endpoint に自動適用される

---

## File-by-file Changes

### Backend

#### 1. CREATE `backend/app/routers/admin/__init__.py` (新規)

```python
"""Admin router package.

Aggregates admin-only sub-routers under /api/v1/admin/*.
Router-level get_admin_user dependency enforces admin auth for every endpoint
in this package. Individual endpoints must NOT repeat the dependency.
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_admin_user
from app.routers.admin import news_sources, pipeline

admin_router = APIRouter(
    prefix="/api/v1/admin",
    dependencies=[Depends(get_admin_user)],
)

admin_router.include_router(news_sources.router)
admin_router.include_router(pipeline.router)

__all__ = ["admin_router"]
```

#### 2. MOVE `backend/app/routers/news_sources.py` → `backend/app/routers/admin/news_sources.py`

**`git mv` で履歴保持**。移動後に編集:

- **L17**: `router = APIRouter(prefix="/api/v1/sources", tags=["sources"])` → `router = APIRouter(prefix="/sources", tags=["admin:sources"])`
- **L8**: `from app.dependencies import CurrentUser, get_admin_user, get_session` → `from app.dependencies import get_session`
- **L28, L42, L55, L68**: 各 endpoint から `_user: Annotated[CurrentUser, Depends(get_admin_user)]` 引数を削除
- **L32**: `return await service.list_sources()` → `return await service.get_all()` (Service リネームに追随)

#### 3. MOVE `backend/app/routers/pipeline.py` → `backend/app/routers/admin/pipeline.py`

**`git mv`**。移動後に編集:

- **L15**: `router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])` → `router = APIRouter(prefix="/pipeline", tags=["admin:pipeline"])`
- **L6**: `CurrentUser, get_admin_user` import 削除 (`get_session` は残す)
- **L31 (`fetch_news`), L45 (`embed_news`)**: `_user: CurrentUser = Depends(get_admin_user)` 引数削除
- **Annotated スタイル統一** (ついでに):
  - `service: PipelineService = Depends(get_pipeline_service)` → `service: Annotated[PipelineService, Depends(get_pipeline_service)]`
  - `session: AsyncSession = Depends(get_session)` → `session: Annotated[AsyncSession, Depends(get_session)]`
  - `from typing import Annotated` import 追加

#### 4. EDIT `backend/app/main.py`

- **L17-24 import block**: `news_sources, pipeline,` を削除し `admin,` を追加

```python
from app.routers import (
    admin,
    articles,
    categories,
    semantic_search,
    watchlist,
)
```

- **L96-97 相当**: `app.include_router(news_sources.router)` と `app.include_router(pipeline.router)` を削除し、代わりに `app.include_router(admin.admin_router)` を追加
- `semantic_search` 登録順序優先の NOTE コメントは維持

#### 5. EDIT `backend/app/services/news_source.py`

**L15-21** の `list_sources` を以下に置き換え:

```python
async def get_all(self) -> NewsSourceDetailList:
    sources = await self.repo.get_all()
    return NewsSourceDetailList(
        items=[NewsSourceDetail.model_validate(s) for s in sources],
    )
```

(メソッド名変更 + `get_count` 呼び出し削除 + `total=count` 削除)

#### 6. EDIT `backend/app/repositories/news_source.py`

- **L17-20** の `get_count` メソッド全体を削除
- **L2**: `from sqlmodel import func, select` → `from sqlmodel import select` (`func` は `get_count` 専用だったので)

#### 7. EDIT `backend/app/schemas/news_source.py`

- **L12 docstring**: `"""POST /api/v1/sources request body."""` → `"""POST /api/v1/admin/sources request body."""`
- **L34 docstring**: `"""GET /api/v1/sources response wrapper."""` → `"""GET /api/v1/admin/sources response wrapper."""`
- **L37**: `total: int` フィールドを `NewsSourceDetailList` から削除

#### 8. EDIT `backend/app/schemas/pipeline.py`

- docstring 内の `/api/v1/pipeline/fetch` → `/api/v1/admin/pipeline/fetch`
- docstring 内の `/api/v1/pipeline/embed` → `/api/v1/admin/pipeline/embed`
- (正確な行は実装時に確認、Grep で特定)

### Backend Tests

#### 9. EDIT `backend/tests/test_routers/test_news_sources.py`

- **全 13 箇所**の `"/api/v1/sources"` リテラルを `"/api/v1/admin/sources"` に置換
- 置換候補行: L13, L33, L46, L60, L79, L94, L106, L115, L127, L139, L145, L154, L162 (実装時に Grep で再確認)
- **L17 と L36 付近**の `assert data["total"] == 0` / `assert data["total"] == 1` を削除
- テスト名・意図・fixture は変更しない

#### 10. EDIT `backend/tests/test_routers/test_pipeline.py`

- docstring 内 `/api/v1/pipeline` → `/api/v1/admin/pipeline`
- 各エンドポイント呼び出しパス (`/api/v1/pipeline/fetch`, `/api/v1/pipeline/embed`) → `/admin/pipeline/*` に更新
- 正確な行は実装時に Grep で特定

### Frontend

#### 11. EDIT `frontend/src/lib/api-client.ts`

- **L104**: `"/pipeline/fetch"` → `"/admin/pipeline/fetch"`
- **L161**: `"/sources"` → `"/admin/sources"`

#### 12. EDIT `frontend/src/lib/client-api.ts`

- **L59**: `"/pipeline/fetch"` → `"/admin/pipeline/fetch"`
- **L68**: `"/sources"` → `"/admin/sources"`
- **L74**: `"/sources"` → `"/admin/sources"`
- **L81**: `` `/sources/${id}` `` → `` `/admin/sources/${id}` ``
- **L87**: `` `/sources/${id}/toggle` `` → `` `/admin/sources/${id}/toggle` ``

#### 13. REGENERATE `frontend/src/types/generated.ts`

`/gen-types` スキルを実行 (backend 起動状態で)。自動再生成により:
- `/api/v1/sources` 系のパス定義が `/api/v1/admin/sources` に更新
- `/api/v1/pipeline/*` が `/api/v1/admin/pipeline/*` に更新
- `NewsSourceDetailList.total` フィールドが型定義から消失

**手動編集禁止** (CLAUDE.md 規約)。

**`project_news_sources_review.md` 記載のコンテナ経由手順** (backend がコンテナで internal 時):
```bash
docker exec vector-backend-1 python -c "import json; from app.main import app; print(json.dumps(app.openapi()))" > frontend/openapi.json
cd frontend && npm run generate-types:file
npx biome format --write src/types/generated.ts  # openapi-typescript は 4-space, biome は 2-space
rm openapi.json
```

### Specs

#### 14. UPDATE 本 spec ファイル自体

実装完了時に以下のいずれかを実施:
- **Option A (推奨)**: `specs/backlog/admin-router-restructure.md` → `specs/admin-router-restructure.md` にファイル移動、Status を "Implemented YYYY-MM-DD (PR #...)" に更新
- **Option B**: backlog に残したまま先頭に `> Status: Done` banner を追加

#### 15. ANNOTATE `specs/backlog/news-source-list-role-split.md`

ファイル先頭に status banner を追加 (ファイル移動はしない):

```markdown
> **Status**: Done (YYYY-MM-DD). Implemented in PR #... (review/news-sources-router branch).
> Superseded by [`admin-router-restructure.md`](admin-router-restructure.md)
> which generalized the admin-only auth to all admin endpoints.
```

---

## Order of Operations & Commit Boundaries

```
1.  git checkout main && git pull
2.  git checkout -b refactor/extract-admin-router
3.  Backend 変更一式 (#1-#10) を実装
    - `backend/app/routers/admin/` パッケージ作成
    - git mv で news_sources.py と pipeline.py を移動
    - 移動後ファイル編集 + main.py 編集 + service/repo/schema 編集
    - test 2 ファイル編集 (パス + total アサーション)
4.  cd backend && uv run ruff check app/ && uv run ruff format --check app/
5.  uv run pytest tests/ -x -q  # 全 pass 確認
6.  [Commit 1] refactor(backend): move admin endpoints under /api/v1/admin with router-level auth
7.  Backend 起動 (uv run uvicorn app.main:app --reload または docker-compose)
8.  /gen-types スキル実行 (#13)
9.  Frontend パス更新 (#11, #12)
10. cd frontend && npx biome check src/ && npx tsc --noEmit && npm run build
11. [Commit 2] refactor(frontend): update api paths to /admin/* and regenerate types
12. Spec 更新 (#14, #15)
13. [Commit 3] docs(specs): promote admin-router-restructure spec and annotate role-split backlog
14. Manual smoke test (後述)
15. git push -u origin refactor/extract-admin-router
16. gh pr create
```

### Commit 1 が atomic な理由

router 移動、service リネーム、schema フィールド削除、テストパス更新、`total` アサーション削除は**すべて相互依存**している。分割すると:
- router 移動だけ → test がパスと auth 期待でコケる
- service リネームだけ → router 呼び出し元で AttributeError
- `total` 削除だけ → test の assertion 失敗

これらを分離する意味がないため 1 commit に集約する。

### Commit 1 と 2 の間の状態

フロントは一時的に壊れる (新パスを呼ぶ前にバックエンドだけ更新されるため)。同じ PR 内で両方 land するので本番影響なし。CI は PR 全体で回るので実害もない。

---

## Verification

### Backend

```bash
cd /Users/you/Vector/backend
uv run ruff check app/
uv run ruff format --check app/
uv run pytest tests/ -x -q
uv run pytest tests/test_routers/test_news_sources.py tests/test_routers/test_pipeline.py -x -v
```

**確認ポイント**:
- `test_list_sources_empty` が `total` アサーションなしで pass
- `test_list_sources_forbidden_for_non_admin` が 403 を返し続ける (**router-level dep が動作している証拠**)
- `test_missing_auth_headers` が 422 を返す

### OpenAPI スキーマ確認

Backend 起動中:

```bash
curl -s http://localhost:8000/openapi.json | python -m json.tool | grep '"/api/v1/'
```

**期待されるパス**:
- `/api/v1/admin/sources`
- `/api/v1/admin/sources/{source_id}`
- `/api/v1/admin/sources/{source_id}/toggle`
- `/api/v1/admin/pipeline/fetch`
- `/api/v1/admin/pipeline/embed`

**古いパス** (`/api/v1/sources`, `/api/v1/pipeline/*`) が含まれていないこと。

### Frontend

```bash
cd /Users/you/Vector/frontend
npx biome check src/
npx tsc --noEmit
npm run build
```

### Manual Smoke Test

1. Backend + Frontend を起動、admin ユーザーでログイン
2. `/settings` ページで `SourceManager` が読み込めること (GET `/api/v1/admin/sources` が 200)
3. ソース作成 → 201、削除 → 204、toggle → 200 を確認
4. Pipeline fetch/embed を dashboard から実行 → 202/200
5. DevTools Network タブで `/api/proxy/admin/sources` に飛んでいることを確認 (BFF proxy 経由)
6. 非 admin ユーザーで `/settings` アクセス → 403 を確認

### Curl でエッジケース確認

```bash
# 認可未付与 → 422
curl -i http://localhost:8000/api/v1/admin/sources

# 非 admin role → 403 (router-level dep が動作している証拠)
curl -i -H "X-Internal-Secret: <dev-secret>" \
        -H "X-User-ID: 00000000-0000-0000-0000-000000000001" \
        -H "X-User-Role: user" \
        http://localhost:8000/api/v1/admin/sources

# admin role → 200
curl -i -H "X-Internal-Secret: <dev-secret>" \
        -H "X-User-ID: 00000000-0000-0000-0000-000000000001" \
        -H "X-User-Role: admin" \
        http://localhost:8000/api/v1/admin/sources

# 旧パス → 404
curl -i http://localhost:8000/api/v1/sources
```

---

## Critical Files

### 新規作成
- `backend/app/routers/admin/__init__.py`

### 移動 (git mv)
- `backend/app/routers/news_sources.py` → `backend/app/routers/admin/news_sources.py`
- `backend/app/routers/pipeline.py` → `backend/app/routers/admin/pipeline.py`

### 編集
- `backend/app/main.py` (router 登録)
- `backend/app/services/news_source.py` (メソッド改名 + count 計算削除)
- `backend/app/repositories/news_source.py` (`get_count` 削除)
- `backend/app/schemas/news_source.py` (`total` 削除 + docstring)
- `backend/app/schemas/pipeline.py` (docstring)
- `backend/tests/test_routers/test_news_sources.py` (パス + total アサーション)
- `backend/tests/test_routers/test_pipeline.py` (パス)
- `frontend/src/lib/api-client.ts` (2 箇所)
- `frontend/src/lib/client-api.ts` (5 箇所)
- `frontend/src/types/generated.ts` (自動再生成)
- `specs/backlog/news-source-list-role-split.md` (status banner)
- 本ファイル (status 更新または移動)

---

## References to Existing Utilities

- **`get_admin_user`** at `backend/app/dependencies.py:55-64` — router-level 依存で再利用
- **`get_session`** at `backend/app/dependencies.py` — 既存パターンを維持
- **`CurrentUser` dataclass** at `backend/app/dependencies.py:21-26` — admin ルーター移動後は handler 内で直接参照しなくなる (router-level dep が裏で動作するので不要)
- **FastAPI 公式チュートリアル** "Bigger Applications - Multiple Files" の `internal/admin.py` パターンに準拠

---

## Risks & Mitigations

| リスク | 影響 | 対策 |
|---|---|---|
| Router-level dep 伝搬の挙動想定違い | admin 認可バイパス | `test_list_sources_forbidden_for_non_admin` が既存。pass することで伝搬を担保 |
| `_user` パラメータ削除で handler 内参照が壊れる | TypeError | Grep 検証済み: handler body で `_user.` や `current_user.` を使っている箇所はゼロ |
| `total` 削除でフロント壊れる | UI エラー | Grep 検証済み: `sourcesData.total` アクセスはどこにも存在しない |
| `get_count` 削除で他モジュール壊れる | ImportError | Grep 検証済み: 呼び出しは `service.list_sources` 内のみ |
| 型再生成前にフロント path 更新で `tsc` エラー | ビルド失敗 | Order of Operations で順序固定 (backend → gen-types → frontend) |
| Commit 1 と 2 の間で local 開発環境が壊れる | 開発体験低下 | 同一 PR 内で両方 land するので本番影響なし |
| `/admin` プレフィックスの将来硬直性 | URL rename コスト | 業界リサーチで実例なしを確認済。理論的懸念として受容 |

---

## 前提 (実装前の git 状態)

- `review/news-sources-router` は PR #31 で main にマージ済み (2026-04-10)
- 本 refactor は `main` (`a1426b9`) から派生した `refactor/extract-admin-router` ブランチで実施

## 関連する memory エントリ

- `project_news_sources_review.md` — 前身ブランチの状態、`generate-types` のコンテナ経由手順
- `project_keyword_status_filter.md` — 将来の admin router (`keywords`) 追加予定
- `feedback_router_registration_order.md` — FastAPI router 登録順序の注意
- `feedback_vo_boundary.md` — Annotated 記法の優先 (pipeline.py の `Depends()` → `Annotated` 統一の根拠)
- `feedback_layer_architecture.md` — Router/Service/Repository 3 層分離 (本 refactor はこれを維持)
