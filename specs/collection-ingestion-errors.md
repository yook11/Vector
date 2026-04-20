# Phase 2: ingestion Fetcher 例外化 + SourceFetchResult 整理

## 背景

Phase 1(PR #76)で `PermanentFetchError` / `TemporaryFetchError` を
`app/collection/errors.py` に集約した。extraction 側(`ArticleHtmlExtractor`)は
既に例外ベースで一貫しているが、ingestion 側の Fetcher(`BaseRssFetcher` /
`HackerNewsFetcher`)は依然として `SourceFetchResult.success=False` を返す旧スタイル。

Phase 2 ではこれを解消し:
- 失敗を例外で表現(extraction と一貫)
- `SourceFetchResult` から dead field を削除
- caller(`tasks.py:fetch_source_metadata`)を `try/except` パターンに揃える

## スコープ

- `app/collection/ingestion/fetchers/rss/base.py:BaseRssFetcher.fetch` の例外化
- `app/collection/ingestion/fetchers/hacker_news.py:HackerNewsFetcher.fetch` の例外化
- `app/collection/ingestion/persister.py:SourceFetchResult` の最小化
- `app/collection/tasks.py:fetch_source_metadata` の try/except 化
- 関連テスト更新

## スコープ外

- `set_http_cache` の配置見直し(Phase 4 で扱う)
- `ArticleCandidate` の VO 化 / dead field 削除(Phase 3 で扱う)
- Fetcher 内部責務分離(HTTP / Parse / Convert / Persist, Phase 4)

## 設計論点と決定

### 論点 A: feedparser `bozo` の扱い → **PermanentFetchError**

現状 `rss/base.py:159-167` で `feed.bozo and not feed.entries` のとき
`success=False` を返している。

決定: `PermanentFetchError(f"feed parse error: {feed.bozo_exception}")` を raise。

理由: bozo はフィード XML が構造的に破損している状態で、同一 URL の再取得で
解決しない。原因が feed 提供側にあるため retry で隠すのは有害。
頻発するならソース自体の無効化で対処する。

### 論点 B: 304 Not Modified の扱い → **正常系として空リストを返す**

現状 `rss/base.py:129-132` で `result.not_modified = True` を返しているが、
caller(tasks.py)はこのフラグを消費していない(dead)。

決定: 304 は `SourceFetchResult(new_discovered=[])` を返す。
caller では `FetchStatus.SUCCESS` + `articles_count=0` として記録される
(「新着 0 件の成功」と意味論的に等価)。

理由: 304 は retry 軸(Permanent/Temporary)のどちらでもないため、例外にすると
分類軸が汚れる。ログには `logger.info("feed_not_modified", source=...)`
を引き続き出すことで可観測性は確保。

### 論点 C: SourceFetchResult の最終 shape → **new_discovered のみ**

```python
@dataclass
class SourceFetchResult:
    new_discovered: list[DiscoveredArticle] = field(default_factory=list)
```

決定: 以下をすべて削除:
- `source_id`: caller が既に `source.id` を保持(dead)
- `success`: 例外化で不要
- `new_count`: `len(new_discovered)` で導出可
- `skipped_count`: 消費は logger のみ。必要ならログ内のローカル変数で十分
- `error_message`: 例外化で `str(e)` に置き換え
- `etag` / `last_modified`: fetch 内部で `set_http_cache` に渡すだけで caller 不要
- `not_modified`: caller が消費していない(dead)

理由:
- `new_count == len(new_discovered)` という不変条件を別フィールドで持つと
  同期不整合の余地を残す(`feedback_structural_guarantee.md`)
- ingestion の `SourceFetchResult` に必要な情報は「下流 dispatch 対象のリスト」
  だけで、カウント値は可観測性のためだけに存在していた → ログ出力で代替
- 実際の消費先を grep した結果、Redis やメトリクス等の外部消費者はおらず、
  純粋にアプリ内部の戻り値コンテナ

### 論点 D: `set_http_cache` の配置 → **Phase 2 では触らない**

`rss/base.py:155` の fetch 内部から直接 Redis に書く現構造は、etag/last_modified を
`SourceFetchResult` から消したあとも fetch 内部で閉じるため Phase 2 では
動作に支障がない。

決定: Phase 4(Fetcher 内部責務分離 HTTP/Parse/Convert/Persist)で
まとめて扱う。

## エラー分類基準(extraction と揃える)

| 事象 | 例外 | 備考 |
|---|---|---|
| HTTP 403 / 404 / 410 / 451 | `PermanentFetchError` | 恒常的な拒否・消失 |
| HTTP 429 | `TemporaryFetchError` | rate limit、時間を空ければ回復 |
| HTTP 5xx | `TemporaryFetchError` | サーバー側一時障害 |
| `httpx.RequestError`(Timeout/DNS/接続) | `TemporaryFetchError` | ネットワーク一時障害 |
| feedparser `bozo and not feed.entries` | `PermanentFetchError` | フィード構造破損 |
| HTTP 304 Not Modified | 例外ではない | `new_discovered=[]` 返却 |

## caller(`fetch_source_metadata`)のフロー

```python
async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}) as client:
    try:
        source_result = await fetcher.fetch(client, session, source)
        status = FetchStatus.SUCCESS
        error_message = None
    except PermanentFetchError as e:
        # 無駄な retry を防ぐため catch して raise しない
        source_result = SourceFetchResult()
        status = FetchStatus.ERROR
        error_message = str(e)
    except TemporaryFetchError as e:
        # FetchLog を記録してから raise(taskiq に retry を委ねる)
        fetch_log = FetchLog(
            source_id=source.id,
            status=FetchStatus.ERROR,
            articles_count=0,
            error_message=str(e),
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )
        session.add(fetch_log)
        await session.commit()
        if is_last_attempt(ctx):
            logger.warning("fetch_source_metadata_max_retries", source_id=source.id)
            return {"source_id": source.id, "status": "error", "reason": str(e)}
        raise

duration_ms = int((time.monotonic() - start_time) * 1000)

# 成功 or Permanent 失敗のパス
fetch_log = FetchLog(
    source_id=source.id,
    status=status,
    articles_count=len(source_result.new_discovered),
    error_message=error_message,
    duration_ms=duration_ms,
)
session.add(fetch_log)
await session.commit()

for discovered in source_result.new_discovered:
    await fetch_content.kiq(discovered.id)

return {
    "source_id": source.id,
    "new_count": len(source_result.new_discovered),
    "status": status.value,
}
```

ポイント:
- `TemporaryFetchError` は `fetch_content` と同じく `is_last_attempt` で
  最終試行時に飲み込み、それ以外は raise して taskiq に retry を任せる
  (`feedback_error_handling_by_capability.md`: Task = キュー機構)
- `PermanentFetchError` は service 層相当の判断として catch、FetchLog を書いて終了
  (retry させても無駄)

## 実装ステップ

1. `BaseRssFetcher.fetch` の try/except を raise に置き換え、`feedparser.bozo`
   パスも raise に置き換え。`SourceFetchResult` の
   `etag`/`last_modified`/`not_modified` へのアサインを削除(304 は
   空結果で return、ETag/Last-Modified は直接 `set_http_cache` に渡す)
2. `HackerNewsFetcher.fetch` の try/except を raise に置き換え
3. `SourceFetchResult` を `new_discovered` のみに縮小
   - `persist_new_articles` から `source_id`/`new_count`/`skipped_count` への
     アサインを削除、戻り値の shape を変更
4. `fetch_source_metadata` を try/except 化、FetchLog 記録ロジックを更新、
   `TemporaryFetchError` の propagation
5. テスト更新:
   - `tests/test_rss_base.py`: `success=False` / `new_count` / `skipped_count`
     アサーションを例外アサーション / `len(new_discovered)` に変更
   - `tests/test_hacker_news.py`: 同上
   - `tests/test_article_persister.py`: `new_count`/`skipped_count`
     アサーションを `len(new_discovered)` ベースに変更
   - `tests/test_metadata_tasks.py`: モック `SourceFetchResult` の
     shape 変更、例外パスのテスト追加
   - `tests/test_fetch_logs.py`: 同上
   - `tests/test_rss_quantum_insider.py`: `new_count` を
     `len(new_discovered)` に変更

## 検証

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/ -x -q
```

## 関連する既存方針

- `feedback_error_classification_by_cause.md`: エラーは原因の所在で分類
- `feedback_error_handling_by_capability.md`: Service = ビジネス判断、
  Task = キュー機構
- `feedback_structural_guarantee.md`: 不変条件は構造的に強制
- `feedback_aggregate_over_individual_vo.md`: 保証はアグリゲート単位
- `project_error_hierarchy_redesign.md`: PR #53 エラー階層再設計済み
- `specs/collection-errors-common.md`: Phase 1 のプラン(完了)
