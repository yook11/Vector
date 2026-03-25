# 仕様書: MissingGreenlet エラー修正（article.id の事前保存）

## 目的

`analyze_articles` ループ内で `session.commit()` 後に `article.id` にアクセスすると `MissingGreenlet` エラーが発生する問題を修正する。

## 背景

### エラー内容

```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called;
can't call await_only() here.
```

### 原因

`session.commit()` を呼ぶと、SQLAlchemy はセッション内の全オブジェクトの属性を「期限切れ（expired）」にする。その後 `article.id` にアクセスすると、SQLAlchemy が DB から再読み込みを試みるが、非同期セッションでは属性アクセス（`__get__`）に `await` を付けられないため `MissingGreenlet` が発生する。

### エラー発生箇所（2箇所）

1. `ai_analyzer.py:196` — 成功時: `logger.info("article_saved", article_id=article.id)`
2. `ai_analyzer.py:215` — 失敗時: `result.errors.append(f"Article {article.id}: {e}")`

## 変更対象ファイル

- `backend/app/services/ai_analyzer.py` — `analyze_articles` 関数のみ

## 変更内容

ループの先頭で `article.id` をローカル変数に保存し、ループ内の全 `article.id` 参照を置き換える。

### Before

```python
for i, article in enumerate(articles):
    if i > 0:
        await asyncio.sleep(REQUEST_INTERVAL)

    try:
        analysis = await analyze_article(session, article, analyzer)
        if analysis is None:
            result.skipped_count += 1
        else:
            await session.commit()
            result.analyzed_count += 1
            logger.info("article_saved", article_id=article.id)  # ← MissingGreenlet
    except RateLimitError as e:
        await session.rollback()
        result.error_count += 1
        result.errors.append(f"Article {article.id}: {e}")  # ← MissingGreenlet
        ...
        break
    except AnalysisError as e:
        await session.rollback()
        result.error_count += 1
        result.errors.append(f"Article {article.id}: {e}")  # ← MissingGreenlet
        continue
    except Exception as e:
        await session.rollback()
        result.error_count += 1
        result.errors.append(f"Article {article.id}: {e}")  # ← MissingGreenlet
        ...
        continue
```

### After

```python
for i, article in enumerate(articles):
    if i > 0:
        await asyncio.sleep(REQUEST_INTERVAL)

    article_id = article.id  # ← commit/rollback 前に保存

    try:
        analysis = await analyze_article(session, article, analyzer)
        if analysis is None:
            result.skipped_count += 1
        else:
            await session.commit()
            result.analyzed_count += 1
            logger.info("article_saved", article_id=article_id)
    except RateLimitError as e:
        await session.rollback()
        result.error_count += 1
        result.errors.append(f"Article {article_id}: {e}")
        ...
        break
    except AnalysisError as e:
        await session.rollback()
        result.error_count += 1
        result.errors.append(f"Article {article_id}: {e}")
        continue
    except Exception as e:
        await session.rollback()
        result.error_count += 1
        result.errors.append(f"Article {article_id}: {e}")
        ...
        continue
```

### 変更のポイント

- `article_id = article.id` をループ先頭（`asyncio.sleep` の後、`try` の前）に配置
- ループ内の `article.id` を全て `article_id` に置き換え（成功ログ + 全 except ブロック）
- `article_id` はただの int 変数なので commit/rollback の影響を受けない

## 変更してはいけないこと

- `analyze_article` 関数（単一記事処理）
- `taskiq_worker.py`（前回の変更で完了済み）
- ループの構造やエラーハンドリングのロジック

## テスト手順

```bash
# ワーカー再ビルド・起動
docker compose stop worker
docker compose up -d --build worker

# ログ監視 — article_saved が出ること、MissingGreenlet が出ないことを確認
docker compose logs -f worker
```

手動タスク投入（cron を待たない場合）:

```bash
docker compose exec backend python -c "
import asyncio
from app.tasks.taskiq_worker import broker, fetch_and_analyze_task
async def main():
    await broker.startup()
    task = await fetch_and_analyze_task.kiq()
    print(f'task_id: {task.task_id}')
    result = await task.wait_result(timeout=300)
    print(f'result: {result.return_value}, err: {result.is_err}')
    await broker.shutdown()
asyncio.run(main())
"
```

DB 確認:

```bash
docker compose exec db psql -U vector -d vector -c "SELECT COUNT(*) FROM analysis_results;"
```

## 成功基準

- [ ] `MissingGreenlet` エラーが発生しない
- [ ] `article_saved` ログが1記事ごとに出力される
- [ ] 分析済み件数が23件から増加する
