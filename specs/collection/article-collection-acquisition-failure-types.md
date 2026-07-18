# stage1 失敗型を 2 ドメイン(read / conversion)に集約し自己記述化する

Status: Implemented

## 目的

collection BC stage1 (`article_acquisition`) の失敗型を、ドメインの 2 活動
(**source を read する / entry を convert する**)に揃え、各型が**監査に焼く情報を
自分で持つ**(現場で詰め、audit は転写するだけ)構造にする。現状の marker 階層
(`SourceAcquisitionError` 中間型 + fetch/read の 2 leaf)は、fetch/read の区別を
origin から marker へ複製した過分解で、命名もドメインを語っていない。これを解く。

PR #687で導入したread originの自己記述化を前提とする。現行契約は
[`read_errors.py`](../../backend/app/collection/article_acquisition/reader/read_errors.py) と
[`test_read_errors.py`](../../backend/tests/collection/article_acquisition/reader/test_read_errors.py) で検証する。
本 spec はその origin を運ぶ **stage 型**の整理。

## stage1 のドメイン失敗は 2 つだけ

| 失敗 | 範囲 | 形 | 理由 |
|---|---|---|---|
| read 失敗 | source 全体 | **例外** | source を読めない → stream を中断・unwind |
| conversion 棄却 | entry 単位 | **値** | 1 件棄却で source を止めない → 継続 |

例外 vs 値の非対称は **abort 意味論**から来る正当な差(値を例外にすると per-entry
継続が壊れる)。DB エラー / 想定外 bug は横断的 infra 失敗で本 spec の対象外。

## 命名(確定)

| 新 | 種別 | 置換対象 |
|---|---|---|
| `AcquisitionReadError(AcquisitionError)` | 例外 | `SourceAcquisitionError` + `AcquisitionExternalFetchError` + `AcquisitionUnreadableResponseError` |
| `AcquisitionConversionRejection` | 値 | `ConversionRejection` (rename) |
| `AcquisitionConversionDefect` | 理由 enum | 維持 |
| `AcquisitionError(VectorDomainError)` | 基底 | 維持 (`STAGE=ACQUISITION` / catch 型) |

- `Acquisition` 接頭で段が揃う(他段 `CurationError`/`AssessmentError` と同規約)
- `Error`(例外) / `Rejection`(値) の接尾で形が分かれる
- `Conversion*` 3 つ(Rejection / Defect)が prefix で結びつく

## 構造変更

### `errors.py`

`AcquisitionReadError` **1 本**に集約。origin を **hold**(コピーしない)し、段の分類は
**construction 時に派生して持つ**:

```python
class AcquisitionReadError(AcquisitionError):
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    origin: ExternalFetchError | UnreadableResponseError
    code: str
    failure_kind: str
    retryability: Retryability

    def __init__(self, *, origin: ExternalFetchError | UnreadableResponseError) -> None:
        super().__init__()
        self.origin = origin
        self.code = origin.CODE                                  # outcome_code (素通し)
        if isinstance(origin, ExternalFetchError):
            self.failure_kind = "external_fetch"
            self.retryability = (
                Retryability.RETRYABLE if origin.retryable else Retryability.NON_RETRYABLE
            )
        else:
            self.failure_kind = "unreadable_response"
            self.retryability = Retryability.NON_RETRYABLE       # read は全 terminal
```

- 中間型 `SourceAcquisitionError` と 2 leaf を撤去。前回の「型が広すぎる(union を
  受ける leaf + 死んだ isinstance)」問題も同時に解消(union は 1 箇所で受ける)。
- `map_origin_to_acquisition` は `AcquisitionReadError(origin=exc)` の一行に縮退
  (型判定は __init__ 内に移動)。service の catch は origin union のまま。
- `failure_kind`/`retryability` は **instance 属性**。`project_marker_failure` は
  getattr で読むため動作不変(現 `AcquisitionExternalFetchError.RETRYABILITY` と同型)。

### `fetched_article_converter.py`

`ConversionRejection` を `AcquisitionConversionRejection` に rename(フィールド不変)。

### `failure_handling.py`

`case SourceAcquisitionError()` → `case AcquisitionError()`(基底で catch、将来の
stage1 例外も拾う)。

## 監査の転写(audit は読むだけ・再分類しない)

`AcquisitionReadError` から:

| audit 列 | 出どころ | 備考 |
|---|---|---|
| `outcome_code` | `marker.code` (= origin.CODE) | read=reason.value / fetch=CODE |
| `failure_kind` | `marker.failure_kind` | 派生済 |
| `retryability` | `marker.retryability` | 派生済 |
| `stage` | ACQUISITION | |
| `error_message` | `redact_secrets(origin._default_message())` | **PII-free 自己記述**(現状の marker repr から変更) |
| `error_chain` | `extract_error_chain(marker)` | marker→origin→cause |
| payload specifics | `marker.origin` から per-type 射影 | 下記 |

specifics の射影(`audit/stages/acquisition.py` の consumer-side helper、origin 型で分岐):
- `UnreadableResponseError` → `read_format` / `read_field` / `read_parser_position`(既存)
- `ExternalFetchError` → `http_status`(既存列を reuse) / `fetch_reason` / `fetch_retry_after_seconds`(**追加**)

`AcquisitionConversionRejection`(値、現状の append_conversion_rejected 経路を rename
追従のみ): `outcome_code` / `conversion_*` / `cause`→chain は現状維持。

### `payloads.py`

`AcquisitionPayload` に additive 追加(JSONB → **migration 不要**):
```python
fetch_reason: str | None = None
fetch_retry_after_seconds: float | None = None
```
`http_status` は既存(acquisition 失敗経路で未使用)を fetch origin.status_code に配線。

## 非自明な設計判断(再 litigate 禁止)

1. **read marker は 1 本**。fetch/read の区別は origin(CODE + 型)が持つ。marker で 2 つに
   割らない(failure_kind を origin 型から派生)。
2. **marker は origin を hold**(flatten/copy しない)。audit は `.origin` を読み通す。同期ずれ無し。
3. **failure_kind/retryability は marker が派生**(origin には載せない)。reader 層が
   audit 語彙(Stage/Retryability)を import しないための層分離。
4. **error_message は `origin._default_message()`**(`str(origin)` ではない)。explicit
   message の PII(例: SSRF の secret 文字列)を**構造的に排除**(default は scalar 合成のみ)。
5. **例外(read) vs 値(conversion) の非対称は意図的**。統一しない。
6. **`AcquisitionError` 基底は維持**(他段規約 + 単一 catch 型)。
7. **fetch specifics は status/reason/retry_after の 3 つだけ列にする**。長い尾
   (redirect_count / bytes / content_type)は error_message のテキストに乗せ、列は作らない
   (consumer-driven: 集計するものだけ列)。

## テスト(red-first・既存 dispatch を oracle に)

- oracle: [test_source_acquisition_failure_dispatch.py](../../backend/tests/collection/test_source_acquisition_failure_dispatch.py)
- `AcquisitionReadError` unit: origin 型ごとに code/failure_kind/retryability が正
  (fetch retryable→RETRYABLE / fetch non-retryable→NON_RETRYABLE / read→NON_RETRYABLE)。
- 監査 e2e: read 失敗→`read_*` + outcome_code=reason.value / fetch 失敗→
  `http_status`+`fetch_reason`+`fetch_retry_after_seconds` + outcome_code=CODE /
  error_message=origin 自己記述 / **SSRF+secret→error_message は CODE のみ**(PII witness)。
- rename 追従: `ConversionRejection`→`AcquisitionConversionRejection` 全箇所。
- 集約: [test_errors.py](../../backend/tests/collection/article_acquisition/test_errors.py) の
  marker 2 leaf テストを 1 クラスへ。

## 非ゴール

- marker 機構ごと撤去(audit-direct = "Design 2")— 本 spec は `AcquisitionReadError`
  を残す保守路線。
- completion の `scrape_failure.py` / その fetch 分類への波及。
- `collection/errors.py` の `SourceFetchError` 旧階層(`external_fetch_errors.py` と二重)—
  生存確認は別タスク。
- data migration(歴史行の outcome_code は据え置き、JSONB additive)。
- fetch の長い尾(redirect/bytes/content_type)の列化。

## 検証

```bash
cd backend
uv run pytest tests/collection/article_acquisition/ tests/audit/ \
  tests/collection/test_source_acquisition_failure_dispatch.py -q
uv run ruff check app/ tests/
uv run ruff format --check <変更ファイル列挙>
uv run pytest tests/ -q -m unit
make test-integration PYTEST_ARGS="tests/collection/test_source_acquisition_failure_dispatch.py -q"
```
