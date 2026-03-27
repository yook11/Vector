# Step 4: レガシーカラム除去

親ドキュメント: [pipeline_architecture.md](../pipeline_architecture.md)

前提: [Step 3](step3-test-rewrite.md) 完了（全テストパス済み）

## 目的

新パイプラインの動作確認が完了した後、不要になったレガシーカラムを DB とコードから除去する。

---

## 除去対象

### DB カラム

| カラム | 型 | 旧用途 | 不要になった理由 |
|---|---|---|---|
| `content` | text | `original_content` の複製 | Phase 4 で `original_content` に一本化済み |
| `content_fetched_at` | timestamptz | 本文取得済み判定 | `original_content IS NOT NULL` で判定可能 |
| `content_fetch_attempts` | int | リトライ回数管理 | SimpleRetryMiddleware が Redis で管理 |

### コード参照箇所（Step 2 で除去済みのはず。残存確認用）

| ファイル | 確認コマンド |
|---|---|
| `app/models/news.py` | `grep -n 'content_fetched_at\|content_fetch_attempts\|^    content:' app/models/news.py` |
| `app/services/content_extractor.py` | `grep -n 'content_fetched_at\|content_fetch_attempts\|\.content =' app/services/content_extractor.py` |
| `app/tasks/` | `grep -rn 'content_fetched_at\|content_fetch_attempts' app/tasks/` |
| `tests/` | `grep -rn 'content_fetched_at\|content_fetch_attempts' tests/` |

---

## Alembic マイグレーション

```python
"""Drop legacy content columns from news_articles."""

def upgrade():
    op.drop_column("news_articles", "content")
    op.drop_column("news_articles", "content_fetched_at")
    op.drop_column("news_articles", "content_fetch_attempts")

def downgrade():
    raise NotImplementedError("Irreversible migration")
```

---

## モデル変更

### `backend/app/models/news.py`

```python
# 除去する定義 (現在 L42-47)
content: str | None = Field(default=None)
content_fetched_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
content_fetch_attempts: int = Field(default=0, nullable=False)
```

---

## 検証項目

- [ ] `grep -rn 'content_fetched_at\|content_fetch_attempts' backend/` でヒットしないこと（テスト含む）
- [ ] `grep -rn '\.content ' backend/app/` でレガシー `content` カラムへの参照がないこと（`original_content` は OK）
- [ ] Alembic マイグレーション適用後、DB に3カラムが存在しないこと
- [ ] `uv run ruff check app/`
- [ ] `uv run pytest tests/ -x -q`
- [ ] `POST /api/v1/news/fetch` → タスク実行 → 記事取得・分析・埋め込みが正常動作すること
