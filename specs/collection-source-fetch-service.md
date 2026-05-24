> **SUPERSEDED (2026-05-25)**: 本 spec の stage1 語彙 (`source_fetch` / `article_collection` /
> `SourceFetch*` / `ingest_source`) は **acquisition** に統一済。正本は
> [`stage1-acquisition-vocabulary-unification.md`](./stage1-acquisition-vocabulary-unification.md)。
>
> 主要語彙 (旧 → 新): `source_fetch` → `acquisition` (stage token / `kind`) /
> `article_collection` → `article_acquisition` (dir) / `SourceFetchError` → `SourceAcquisitionError` /
> `SourceFetchFailureHandler` → `SourceAcquisitionFailureHandler` /
> `SourceFetchAuditRepository` → `SourceAcquisitionAuditRepository` /
> `SourceFetchPayload` → `AcquisitionPayload` / `ingest_source` → `acquire_source` (task) /
> `IngestSourceArg` → `AcquireSourceArg`。`fetch` I/O 基層 (ExternalFetchError / FetchedArticle /
> FetchLog 等) は据え置き。本 spec の当該記述は歴史的経緯として残し、現行仕様としては読まないこと。

# Phase 3: SourceFetchService 分離 + HN 増分取得 state の Redis 化

## 背景

Phase 2(PR #77)で ingestion Fetcher の例外化と `SourceFetchResult` の最小化を
完了したが、`fetch_source_metadata` タスクは依然として 100 行超で、以下の責務が
混在している:

1. NewsSource 存在チェック
2. quota チェック(ビジネス判断)
3. HTTP fetch 実行
4. エラー分類(Permanent / Temporary)と retry 判断
5. FetchLog 記録
6. 下流 dispatch
7. 返り値組み立て

一方、extraction 側は既に `ContentFetchService` + `fetch_content` で
「Service = ビジネス判断、Task = キュー機構」の形に整理されている
(`feedback_error_handling_by_capability.md`)。

Phase 3 では ingestion 側も同じ形に揃える:
- `SourceFetchService` を新設し、Service にユースケース実行を集約
- FetchLog 書き込みは Task 層の責務として明確化(実行結果の記録 = キュー機構側)
- あわせて、現状 FetchLog から導出している HN の増分取得 state を Redis に移動
  する(設計負債の解消)

## スコープ

- `app/collection/ingestion/service.py` 新設(`SourceFetchService`)
- `app/collection/ingestion/fetchers/hn_fetch_state.py` 新設
  (HN 固有の増分取得 Redis state)
- `app/collection/ingestion/fetchers/hacker_news.py` を新 state に書き換え
- `app/collection/ingestion/fetchers/source_helpers.py` 削除
  (FetchLog から `last_successful_fetch_at` を算出する関数の除去)
- `app/collection/ingestion/persister.py` の `SourceFetchResult` を
  `PersistResult` にリネーム(Service 返り値と名前衝突を避ける)
- `app/collection/tasks.py:fetch_source_metadata` を薄くする
- 関連テスト更新 + 新規追加

## スコープ外

- `NewsSource` モデルへの `last_fetched_at` カラム追加
  → 「ソースの本質的属性ではない」という判断で不採用。fetcher 固有の増分取得 state は
  Redis に寄せる
- `dispatch_sources` 側の quota 事前チェック
- Phase 4(Fetcher 内部責務分離 HTTP / Parse / Convert / Persist)

## 設計論点と決定

### 論点 A: HN の増分取得 state をどこに持つか → Redis(`hn_fetch_state.py`)

現状 `source_helpers.py:get_last_successful_fetch_at` が `fetch_logs` テーブルから
`MAX(fetched_at)` を算出しているが、これは業務状態をログに依存させる設計。

決定:
- RSS の `http_cache.py`(ETag / Last-Modified を Redis に保存)と対称に、
  HN 専用の Redis ヘルパー `hn_fetch_state.py` を作る
- キー: `hn_fetch_state:{source_id}`、値: 最終フェッチ時刻の ISO 文字列
- TTL なし(成功時に上書きされ続ける)
- fetcher の内部状態として閉じる(Service/Task は知らない、現状の ETag/Last-Modified と同じ扱い)

理由:
- `last_fetched_at` は「HN API の `created_at_i>` フィルタに使う増分取得キー」であって、
  全 fetcher 共通の概念ではない(RSS は ETag/Last-Modified で代替)
- FetchLog は本来「運用ログ」であり、業務ロジックから参照されるべきではない
  (将来のログ削除/クリーンアップで業務が壊れるのを防ぐ)

### 論点 B: Service の責務 → ユースケース実行に純化

`SourceFetchService.execute(source_id) -> SourceFetchResult`

Service がやること:
1. NewsSource を読み込む(無ければ `status="not_found"` を返す)
2. `DAILY_REQUEST_LIMIT` を持つ fetcher に対して quota チェック
   (超過していれば `status="skipped_quota"` を返す)
3. `fetcher.fetch(client, session, source)` を呼ぶ
4. session を commit(新規 `DiscoveredArticle` の永続化)
5. `SourceFetchResult(status="fetched", new_discovered=[...])` を返す

Service がやらないこと:
- FetchLog の書き込み(Task 層の責務)
- 下流 `fetch_content.kiq` dispatch(Task 層の責務)
- retry 判断(Task 層の責務)
- HTTP クライアントの生成(引数で受け取る or 内部で生成のどちらかを決める)

### 論点 C: FetchLog 書き込みの位置 → Task 層

「実行結果を記録する」責務は Task に属する(`fetch_content` 同様、taskiq の
ライフサイクルと結合している)。Task 側にヘルパー関数を置いて繰り返しを減らす。

### 論点 D: Service 返り値型 → リネームして名前衝突を回避

Service の返り値として `SourceFetchResult` を使いたいが、既存の
`persister.py` に同名のクラスがあるため衝突する。

決定:
- 既存 `persister.py:SourceFetchResult`(フィールド `new_discovered` のみ)
  → `PersistResult` にリネーム(永続化の内部結果であることを明示)
- Service 返り値を新しく `SourceFetchResult` として `service.py` に定義

```python
# ingestion/service.py
@dataclass(frozen=True)
class SourceFetchResult:
    status: Literal["fetched", "not_found", "skipped_quota"]
    new_discovered: list[DiscoveredArticle] = field(default_factory=list)
```

### 論点 E: HTTP クライアント生成の位置 → Service 内部

現状は Task 層で `async with httpx.AsyncClient(...)` を開いていたが、
`ContentFetchService` のパターン(Service が `ArticleHtmlExtractor` を受け取り、
Extractor 内で client を生成)と揃えるなら、Service 側で client を生成する。

決定: Service が `async with httpx.AsyncClient(headers=...)` を開く。
Task は `session_factory` を Service に渡すだけ。

## 新しい fetch_source_metadata の形

```python
@broker_metadata.task(
    task_name="fetch_source_metadata",
    timeout=300,
    max_retries=2,
    retry_on_error=True,
)
async def fetch_source_metadata(source_id: int, ctx: Context = TaskiqDepends()) -> dict:
    session_factory = ctx.state.session_factory
    svc = SourceFetchService(session_factory)
    start = time.monotonic()

    try:
        result = await svc.execute(source_id)
    except PermanentFetchError as e:
        await _record_fetch_log(session_factory, source_id, FetchStatus.ERROR, 0, str(e), start)
        return {"source_id": source_id, "status": "error", "reason": str(e)}
    except TemporaryFetchError as e:
        await _record_fetch_log(session_factory, source_id, FetchStatus.ERROR, 0, str(e), start)
        if is_last_attempt(ctx):
            return {"source_id": source_id, "status": "error", "reason": str(e)}
        raise

    if result.status == "not_found":
        return {"source_id": source_id, "status": "not_found"}
    if result.status == "skipped_quota":
        return {"source_id": source_id, "status": "skipped", "reason": "daily_quota"}

    new_count = len(result.new_discovered)
    await _record_fetch_log(session_factory, source_id, FetchStatus.SUCCESS, new_count, None, start)

    for d in result.new_discovered:
        await fetch_content.kiq(d.id)

    return {"source_id": source_id, "new_count": new_count, "status": "success"}
```

## 実装ステップ

1. `hn_fetch_state.py` 新設(Redis helper)
   - `get_last_fetched_at(source_id) -> datetime | None`
   - `set_last_fetched_at(source_id, ts: datetime) -> None`
2. `hacker_news.py` 変更
   - `source_helpers.get_last_successful_fetch_at` 参照 → `hn_fetch_state.get_last_fetched_at`
   - 成功時に `set_last_fetched_at(source.id, datetime.now(UTC))` を呼ぶ
3. `source_helpers.py` 削除
4. `persister.py:SourceFetchResult` → `PersistResult` にリネーム、
   `rss/base.py`, `hacker_news.py`, `registry.py` の型参照/import を修正
5. `service.py` 新設 — `SourceFetchService` + 新しい `SourceFetchResult`
6. `tasks.py` 変更
   - `_record_fetch_log` ヘルパー関数を追加
   - `fetch_source_metadata` を Service 呼び出しに整理
   - `httpx.AsyncClient` 生成を Service に移動
7. テスト更新/新規
   - 新規 `test_hn_fetch_state.py`(Redis helper 単体)
   - 新規 `test_source_fetch_service.py`(Service の各ステータスパス)
   - 更新 `test_hacker_news.py`(last_fetched_at の Redis 参照/書き込み)
   - 更新 `test_metadata_tasks.py`(Service 分離後の Task 形)
   - 更新 `test_fetch_logs.py`(Task が FetchLog を書く構造)
   - 更新 `test_article_persister.py`(`PersistResult` へのリネーム)

## 検証

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run pytest tests/ -x -q
```

## 影響範囲

| カテゴリ | ファイル | 変更種別 |
|---|---|---|
| app(新設) | `ingestion/service.py` | 新規 |
| app(新設) | `ingestion/fetchers/hn_fetch_state.py` | 新規 |
| app(削除) | `ingestion/fetchers/source_helpers.py` | 削除 |
| app(変更) | `ingestion/persister.py` | リネーム |
| app(変更) | `ingestion/fetchers/hacker_news.py` | state 参照変更 + 型変更 |
| app(変更) | `ingestion/fetchers/rss/base.py` | 型変更のみ |
| app(変更) | `ingestion/registry.py` | 型変更のみ |
| app(変更) | `collection/tasks.py` | 大幅短縮 |
| tests(新設) | `test_hn_fetch_state.py`, `test_source_fetch_service.py` | 新規 |
| tests(変更) | `test_hacker_news.py`, `test_metadata_tasks.py`, `test_fetch_logs.py`, `test_article_persister.py` | 更新 |

## 関連する既存方針

- `feedback_error_handling_by_capability.md`: Service = ビジネス判断、
  Task = キュー機構
- `feedback_service_atomic_usecase.md`(関連: `project_service_atomic_usecase.md`)
- `feedback_session_factory_di.md`: Service は `session_factory` を DI
- `feedback_db_design_domain_driven.md`: DB はドメインモデルに従って設計
  (FetchLog を業務判断に使わない根拠)
- `feedback_code_should_express_business_importance.md`: コード構造で
  ビジネス重要度を表現(増分取得キーが fetcher 固有であることを構造で示す)
- `feedback_redis_infra_placement.md`: Redis 操作はドメインに配置、
  接続は `app/redis.py`
- `specs/collection-ingestion-errors.md`: Phase 2(完了)
