# stage1 語彙統一: acquisition (authority spec)

**Status: Implemented / authority** (2026-05-25 確定)。collection BC stage1 の語彙の正本。
旧 `source_fetch` / `article_collection` / `SourceFetch*` / `ingest_source` を語る
spec はすべて本 spec に superseded される。

## 確定モデル: 2層

collection BC は2工程。stage1 の identity 語彙を **acquisition** に統一し、低レベル
I/O 動詞 **fetch** をその基層として残す。stage2 (completion/scrape on fetch) と対称。

```
stage1 = acquisition 工程   ┐
stage2 = completion 工程     ├─ 共通の汎用 fetch I/O 基層 の上に立つ
        (内部 sub = scrape) ┘
```

- **acquisition** = 「記事(article)そのものをシステムに獲得する」工程 / BC / 監査 identity。
- **fetch** = HTTP/transport/read の I/O 動詞。工程語彙ではない。共用・凍結。

## 名称マッピング (旧 → 新)

| レイヤー | 旧 | 新 |
|---|---|---|
| dir / package | `app/collection/article_collection/` | `app/collection/article_acquisition/` |
| service | `ArticleAcquisitionService` | (不変、アンカー) |
| 失敗 marker (stage1 専用) | `SourceFetchError` | `SourceAcquisitionError` |
| 失敗 handler | `SourceFetchFailureHandler` | `SourceAcquisitionFailureHandler` |
| 監査 repo | `SourceFetchAuditRepository` | `SourceAcquisitionAuditRepository` |
| 観測 payload class | `SourceFetchPayload` | `AcquisitionPayload` |
| task arg | `IngestSourceArg` | `AcquireSourceArg` |
| 観測 stage token (DB) | `stage="source_fetch"` / `Stage.SOURCE_FETCH` | `stage="acquisition"` / `Stage.ACQUISITION` |
| payload discriminator (JSONB) | `kind="source_fetch"` | `kind="acquisition"` |
| taskiq task (wire) | `task_name="ingest_source"` / `ingest_source()` | `acquire_source` |
| structlog events | `ingest_source_started/completed/unexpected_error`, `source_fetch_failure_audit_dropped` | `acquire_source_*`, `source_acquisition_failure_audit_dropped` |

DB token (`stage`/`kind`) は Alembic migration `v1_acquisition_stage_rename` で移行
(CHECK 制約 drop/recreate + `UPDATE stage` + `jsonb_set` payload kind)。
round-trip 検証済 (upgrade: source_fetch→acquisition / content_fetch 不変、downgrade: 逆)。

## KEEP (fetch I/O 基層・共用・凍結。acquisition 化しない)

- `ExternalFetchError` + `Fetch*Error` 18種 (`external_fetch_errors.py`、**stage2 と共用**)
- `app/collection/errors.py` (`SourceFetchError` 基底 / `Permanent`/`Temporary*FetchError`、
  共用 HTTP I/O 例外階層。stage1 marker `SourceAcquisitionError` とは別物)
- `FetchedArticle` / `fetch_articles` / `fetched_article_converter` / `FetchedArticleConversionError`
  (Reader I/O 結果ゆえ正当な fetch)
- `RawHttpClient.fetch` / reader `.fetch()` / `UnreadableResponseError` / `ConversionReason`
- `FetchLog` / `FetchStatus` / `_record_fetch_log` (別 DB テーブル `fetch_logs`、I/O 結果ログ)
- `fetcher_class` JSONB key (`AcquisitionPayload` 内、vestigial)
- 親 `app/collection/` (収集 BC umbrella、stage1+stage2 共通の傘)
- 内部ファイル名 (`service.py`/`errors.py`/`fetcher.py`/`fetched_article.py`/`reader/`/`tools/` 等)。dir 名のみ変更

## deploy (stop-the-world)

CHECK は新値のみ + taskiq token も変わるため旧/新 worker 混在不可。
全 worker/scheduler 停止 → queue drain → `alembic upgrade head` → 新 image 起動。
`AcquireSourceArg` fields `{id,name}` 不変 = message shape 無回帰。

## 経緯

stage1 dir は #607 で `source_fetch`→`article_collection` に改名されたが、本来 acquisition
にしたかった (当時 acquisition は stage2 が使用していて衝突)。stage2 の acquisition 剥がし
(#612, scrape へ移譲) で acquisition が空き、本 spec で stage1 に統一した。
