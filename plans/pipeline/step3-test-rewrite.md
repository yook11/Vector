# Step 3: テスト改修

親ドキュメント: [pipeline_architecture.md](../pipeline_architecture.md)

前提: [Step 2](step2-task-splitting.md) 完了（新パイプライン動作確認済み）

## 目的

モノリシックタスク前提のテストを、チェーンタスク構造に合わせて書き直す。
変更不要なテストはそのまま残す。

---

## テストファイル影響度

| ファイル | 影響度 | 対応 |
|---|---|---|
| `tests/test_taskiq_worker.py` | HIGH | 全面書き直し → `tests/test_pipeline_tasks.py` |
| `tests/test_content_extractor.py` | MODERATE | L427 の `content_fetch_attempts` アサーション修正 |
| `tests/test_routers/test_news.py` | MODERATE | POST /fetch のモック対象変更 |
| `tests/test_ai_analyzer.py` | なし | 変更不要 |
| `tests/test_news_fetcher.py` | なし | 変更不要 |
| `tests/test_hacker_news.py` | なし | 変更不要 |
| `tests/test_alpha_vantage.py` | なし | 変更不要 |
| `tests/test_dedup.py` | なし | 変更不要 |
| `tests/test_semantic_search.py` | なし | 変更不要 |
| `tests/conftest.py` | なし | フィクスチャ定義にレガシーカラム直接参照なし |

---

## 1. `tests/test_pipeline_tasks.py` — 新規作成

`test_taskiq_worker.py` を置き換える。タスクごとに独立したテストクラス:

### テスト構成

```
TestFetchMetadata
  - test_fetches_all_active_sources
  - test_fetches_specified_source_ids
  - test_dispatches_pending_after_fetch
  - test_handles_fetch_errors

TestDispatchPending
  - test_enqueues_fetch_content_for_pending_articles
  - test_enqueues_analyze_for_content_without_analysis
  - test_enqueues_embed_for_analysis_without_embedding
  - test_skips_articles_with_skip_content_fetch

TestFetchContent
  - test_fetches_and_stores_content
  - test_chains_analyze_article_on_success
  - test_skips_already_fetched (冪等性)
  - test_permanent_error_sets_skip_flag
  - test_temporary_error_raises_for_retry
  - test_last_retry_sets_skip_flag
  - test_quality_gate_rejects_short_content

TestAnalyzeArticle
  - test_analyzes_and_stores_result
  - test_chains_generate_embedding_on_success
  - test_skips_already_analyzed (冪等性)
  - test_safety_block_clears_content
  - test_temporary_error_raises_for_retry

TestGenerateEmbedding
  - test_generates_and_stores_embedding
  - test_skips_already_embedded (冪等性)
  - test_temporary_error_raises_for_retry
```

### モック方針

- ブローカー: `broker.task` のモックは不要。タスク関数を直接呼び出す
- DB: `conftest.py` の既存 `db_session` フィクスチャを使用
- 外部 API: `AsyncMock` でサービス関数をパッチ
- チェーン呼び出し: `.kiq()` をモックして呼び出し確認

```python
# チェーン呼び出しの検証例
@patch("app.tasks.pipeline_tasks.analyze_article")
async def test_chains_analyze_on_success(mock_analyze, db_session):
    mock_analyze.kiq = AsyncMock()
    await fetch_content(article_id=42)
    mock_analyze.kiq.assert_called_once_with(42)
```

---

## 2. `tests/test_content_extractor.py` — 修正

### L427: `content_fetch_attempts` アサーション

```python
# 変更前
assert bad_article.content_fetch_attempts == 1

# 変更後: レガシーカラムではなく、original_content が None のままであることを確認
assert bad_article.original_content is None
```

### レガシーカラム書き込みテストの除去

`content`, `content_fetched_at` の設定を検証しているテストがあれば `original_content` の検証に統一する。

---

## 3. `tests/test_routers/test_news.py` — 修正

### POST /fetch のモック対象変更

```python
# 変更前
@patch("app.routers.news.fetch_and_analyze_task")

# 変更後
@patch("app.routers.news.fetch_metadata")
```

レスポンス構造（`NewsFetchResponse`）は変更なし。`job_id` の検証もそのまま。

---

## 4. `tests/test_taskiq_worker.py` — 削除

`test_pipeline_tasks.py` に完全に置き換えられるため削除する。

---

## 検証項目

- [ ] `uv run pytest tests/ -x -q` が全件パスすること
- [ ] `uv run ruff check app/ tests/` がエラーなしであること
- [ ] 新テストが冪等性・エラーハンドリング・チェーン呼び出しをカバーしていること
