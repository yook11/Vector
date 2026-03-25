# タスク2: AIモデルの正規化と「1対多」分析への対応 — 実装計画

## Context

`analyses` テーブルの `ai_provider`/`ai_model` が文字列のまま保存されており、冗長かつ表記揺れリスクがある。また `news_article_id` に UNIQUE 制約があり、1記事1分析しか保持できない。将来の複数モデル分析に対応するため、正規化テーブル `ai_models` を導入し、1:N 関係に変更する。

**確定済み設計判断:**
- APIレスポンスにAIモデル情報を含める（管理用途）
- 通常APIはデフォルトモデルの結果1件のみ返す
- フロントエンドにモデル情報は非表示
- `is_default`/`is_evaluation` はDBに持たず `config.py`（環境変数）で管理
- `ai_models` テーブル: `id, provider, name, is_active` + `UNIQUE(provider, name)`

---

## 実装ステップ（依存順）

### ~~Step 1: config.py に設定追加~~ ✅ 実装済み

**変更ファイル:**
- `backend/app/config.py` — `ai_model_name`, `default_ai_model_id`, `evaluation_ai_model_id` 追加
- `backend/app/services/gemini_analyzer.py` — `GEMINI_MODEL` 定数削除 → `settings.ai_model_name` に置換（3箇所: property, log, API call）

---

### ~~Step 2: AIModel モデル新規作成~~ ✅ 実装済み

**変更ファイル:**
- `backend/app/models/ai_model.py` (NEW) — `AIModel(SQLModel, table=True)`, fields: id/provider/name/is_active, UNIQUE(provider, name), Relationship → analyses
- `backend/app/models/__init__.py` — `AIModel` インポート・`__all__` 追加

---

### ~~Step 3: AnalysisResult モデル変更~~ ✅ 実装済み

**変更ファイル:** `backend/app/models/analysis.py`
- `ai_provider`/`ai_model` カラム削除 → `ai_model_id` FK (`ondelete=RESTRICT`) 追加
- `news_article_id` の `unique=True` 削除
- `__table_args__`: `UniqueConstraint("news_article_id", "ai_model_id")` + `Index("idx_analyses_ai_model_id")` 追加
- Relationship: `ai_model: "AIModel"` 追加、`back_populates="analysis"` → `"analyses"` に修正
- forward ref: `from app.models.ai_model import AIModel` 追加

---

### ~~Step 4: NewsArticle リレーション変更 (1:1 → 1:N)~~ ✅ 実装済み

**変更ファイル:** `backend/app/models/news.py`
- `analysis: "AnalysisResult"` (uselist=False) → `analyses: list["AnalysisResult"]` に変更

**影響箇所（Step 8, 9 で対応）:**
- `routers/news.py`: `article.analysis` → `_get_default_analysis(article)`
- `scripts/compare_models.py`: `article.analysis` → `article.analyses`
- eager load: `selectinload(NewsArticle.analysis)` → `selectinload(NewsArticle.analyses)`

---

### ~~Step 5: Alembic マイグレーション~~ ✅ 実装済み・検証済み

**変更ファイル:** `backend/alembic/versions/a5_normalize_ai_model.py` (NEW)
- Revision: `a5b6c7d8e9f0`, down_revision: `a4b5c6d7e8f0`

**実機確認結果:**
- 制約名 `analyses_news_article_id_key` を `\d analyses` で確認済み（計画通り）
- `alembic upgrade head` → 成功
- `alembic downgrade -1 && alembic upgrade head` 往復テスト → 成功

**upgrade() 実装内容:**
1. `ai_models` テーブル作成（id, provider, name, is_active + UNIQUE）
2. 既存 analyses から DISTINCT でシード（`ON CONFLICT DO NOTHING`）
3. `ai_model_id` (nullable) 追加 → バックフィル → NOT NULL 化 + FK (`fk_analyses_ai_model_id`, RESTRICT)
4. `analyses_news_article_id_key` UNIQUE 削除 → `uq_analyses_article_model` 複合 UNIQUE 追加
5. `idx_analyses_ai_model_id` インデックス追加
6. `ai_provider`, `ai_model` カラム削除

**downgrade():** 逆順で完全復元

---

### ~~Step 6: Schema 変更 (API契約)~~ ✅ 実装済み
**File:** `backend/app/schemas/analysis.py`

```python
class AIModelBrief(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    id: int
    provider: str
    name: str

class AnalysisResponse(BaseModel):
    ...
    # ai_provider: str  ← 削除
    ai_model: AIModelBrief  # ← 追加（JSON: aiModel: {id, provider, name}）
    ...
```

**File:** `backend/app/schemas/__init__.py` — `AIModelBrief` を追加

---

### ~~Step 7: Service 変更~~ ✅ 実装済み
**File:** `backend/app/services/ai_analyzer.py`

**7a. `_resolve_ai_model_id()` ヘルパー追加:**
- `provider` + `model_name` で `ai_models` を検索
- **見つからなければ `ValueError` を投げる**（自動作成しない）
- モデル登録はマイグレーション（seed data）か管理操作で明示的に行う
- typo や未登録モデルの使用をフェイルファストで検出

**7b. `analyze_article()` 変更:**
- 引数に `ai_model_id: int | None = None` 追加
- `ai_model_id` が未指定なら `_resolve_ai_model_id()` で解決
- 「既に分析済み」チェック (L129): `news_article_id` **AND** `ai_model_id` で検索に変更
- `AnalysisResult` 生成 (L170-178): `ai_provider`/`ai_model` → `ai_model_id` に置換

**7c. `analyze_articles()` 変更:**
- 引数に `ai_model_id: int | None = None` 追加、`analyze_article()` に伝播

---

### ~~Step 8: Router 変更~~ ✅ 実装済み
**File:** `backend/app/routers/news.py`

**8a. ヘルパー追加（簡素化版）:**
```python
def _get_default_analysis(article: NewsArticle) -> AnalysisResult | None:
    return article.analyses[0] if article.analyses else None
```
ループ不要 — Step 8b の filtered eager load でデフォルトモデルの分析のみロード済みのため。

**8b. `_news_eager_options()` 更新 (L120-133):**
- `selectinload(NewsArticle.analysis)` → **filtered eager load** に変更 (2箇所):
  ```python
  selectinload(
      NewsArticle.analyses.and_(
          AnalysisResult.ai_model_id == settings.default_ai_model_id
      )
  )
  ```
  これにより `article.analyses` にはデフォルトモデルの結果のみがロードされる。
- `selectinload(AnalysisResult.ai_model)` を追加（AIModel の eager load）

**8c. `_build_news_response()` 更新 (L83-103):**
- `article.analysis` → `_get_default_analysis(article)`
- `ai_provider=a.ai_provider` → `ai_model=AIModelBrief(id=a.ai_model.id, provider=a.ai_model.provider, name=a.ai_model.name)`

**8d. 全 JOIN 条件にモデルフィルタ追加 (L177-230):**
4箇所の JOIN すべてに `& (AnalysisResult.ai_model_id == settings.default_ai_model_id)` を追加

---

### ~~Step 9: Worker 変更~~ ✅ 実装済み
**File:** `backend/app/tasks/taskiq_worker.py`

Phase 4 (L276-303): outer join 条件にモデルフィルタ追加
```python
.outerjoin(
    AnalysisResult,
    (AnalysisResult.news_article_id == NewsArticle.id)
    & (AnalysisResult.ai_model_id == settings.default_ai_model_id),
)
```
`analyze_articles()` 呼び出しに `ai_model_id=settings.default_ai_model_id` を渡す。

**評価モデルフロー (Phase 4b):** 今回は schema 対応のみ。以下は別タスクで実装：
- Worker 内の評価分岐ロジック（`evaluation_ai_model_id` が設定されている場合のみ実行）
- 別プロバイダー用の analyzer 実装（例: OpenAI）
- 比較用エンドポイント or スクリプト
- 運用フロー（評価結果のレビュー・デフォルト切替）

---

### ~~Step 10: compare_models.py 更新~~ ✅ 実装済み
**File:** `backend/app/scripts/compare_models.py`

**確認済み:** ファイルは存在する（`backend/app/scripts/compare_models.py`）。

- L181: `selectinload(NewsArticle.analysis)` → `selectinload(NewsArticle.analyses)`
  - `.selectinload(AnalysisResult.translations)` チェーンもそのまま維持
- L230: `flash_result=article.analysis` → `flash_result=article.analyses[0] if article.analyses else None`
  - 既存コードが `where(NewsArticle.id.in_(select(AnalysisResult.news_article_id)))` で分析済み記事のみ取得しているため、`analyses[0]` で安全
  - 将来、複数モデル分析が入った場合はモデルID指定フィルタが必要になるが、現時点では不要

---

### ~~Step 11: テスト更新~~ ✅ 実装済み
**File:** `backend/tests/conftest.py`
- `AIModel` インポート追加
- `sample_ai_model` fixture 追加（`provider="gemini", name="gemini-2.0-flash"`）

**File:** `backend/tests/test_ai_analyzer.py`
- 全テストで `sample_ai_model` fixture を使用
- `ai_provider="gemini", ai_model="gemini-2.0-flash"` → `ai_model_id=sample_ai_model.id` に置換
- アサーション: `result.ai_provider == "gemini"` → `result.ai_model_id == sample_ai_model.id`
- L527: `assert "aiModel" not in data["analysis"]` → `assert "aiModel" in data["analysis"]` に変更

**File:** `backend/tests/test_routers/test_news.py`
- `_create_analysis()` ヘルパー: `ai_provider`/`ai_model` → `ai_model_id` に変更
- テスト前に AIModel レコードを作成する fixture を追加

---

### ~~Step 12: Frontend 変更~~ ✅ 実装済み

**確認済み:** `aiProvider` は以下2箇所のみ参照:
1. `frontend/src/components/news/NewsDetail.tsx:97` — 表示テキスト（手動変更）
2. `frontend/src/types/generated.ts:428` — 型定義（自動再生成で対応）

**File:** `frontend/src/components/news/NewsDetail.tsx` (L96-99)
```tsx
// Before:
Analyzed by {analysis.aiProvider} at {formatDate(analysis.analyzedAt)}

// After:
Analyzed at {formatDate(analysis.analyzedAt)}
```

**File:** `frontend/src/types/index.ts`
- 型 regenerate 後に narrowing が正しく動くか確認（`sentiment` のみ narrowing なので問題ない想定）

**コマンド:** `npm run generate-types` で `generated.ts` を再生成（`aiProvider` → `aiModel` の型変更が自動反映される）

---

### ~~Step 13: ドキュメント更新~~ ✅ 実装済み
**File:** `docs/02_DATABASE_DESIGN.md`
- `ai_models` テーブル追加
- `analyses` テーブルからカラム削除・FK追加・制約変更を反映
- ER図: `news_articles ||--o| analyses` → `news_articles ||--o{ analyses`
- `ai_models ||--o{ analyses` 追加

---

## 検証プロトコル ✅ 全パス

```bash
# Backend
cd backend && ruff check app/ && ruff format --check app/ && python -m pytest tests/ -x -q

# Frontend
cd frontend && npx eslint src/ && npx tsc --noEmit

# マイグレーション（Docker環境）
cd backend && alembic upgrade head
cd backend && alembic downgrade -1 && alembic upgrade head  # 往復テスト
```

**結果:**
- ruff check: All checks passed
- ruff format: 47 files already formatted
- pytest: 172 passed (36s)
- ESLint: OK
- tsc --noEmit: OK
- マイグレーション往復テスト: OK（Step 5 で確認済み）

---

## 影響サマリ

| 変更対象 | ファイル数 | 破壊的変更 |
|---|---|---|
| Models | 3 (ai_model.py[NEW], analysis.py, news.py) | YES |
| Schemas | 2 (analysis.py, __init__.py) | YES (API) |
| Config | 1 (config.py) | NO |
| Services | 1 (ai_analyzer.py) | NO |
| Router | 1 (news.py) | YES (API) |
| Worker | 1 (taskiq_worker.py) | NO |
| Script | 1 (compare_models.py) | NO |
| Migration | 1 (a5[NEW]) | N/A |
| Tests | 3 (conftest.py, test_ai_analyzer.py, test_news.py) | N/A |
| Frontend | 2 (NewsDetail.tsx, types再生成) | YES (UI) |
| Docs | 1 (02_DATABASE_DESIGN.md) | N/A |
| **合計** | **17ファイル** | |
