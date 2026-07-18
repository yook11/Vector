# pipeline_events 失敗属性 projection 仕様

旧分類 enum に混在していた意味を分解し、各 stage の error class / adapter を
監査情報の SSoT にするための仕様。

Status: Implemented (Scope 6 applied; legacy top-level columns removed)

## 目的

監査行から次の 2 点を直接読めるようにする。

1. どの段階のイベントか
2. どんな失敗が起きたか

その上で、retry 可否や後続処理は「失敗の身元」と混ぜず、別属性として投影する。

## 原則

### 1. stage は親 class が持つ

stage は個別エラーではなく、stage error root の責務。

```python
class CurationError(VectorDomainError):
    STAGE: ClassVar[Stage] = Stage.CURATION
```

`CurationTerminalDropError` / `CurationRecoverableError` などはこの値を継承する。

外部例外 (`SQLAlchemyError`, `openai.APIError`, `pydantic.ValidationError` など) は
stage を持たないため、stage audit repository / adapter 側が stage 文脈を与える。

### 2. failure_kind は stage marker が持つ

`failure_kind` は「stage 内でどの失敗判断に分類されたか」を表す安定 slug。
Python class 名そのものではなく、rename に耐える audit 契約値とする。

```python
class CurationTerminalDropError(CurationError):
    FAILURE_KIND: ClassVar[str] = "terminal_drop"


class CurationTerminalKeepError(CurationError):
    FAILURE_KIND: ClassVar[str] = "terminal_keep"


class CurationRecoverableError(CurationError):
    FAILURE_KIND: ClassVar[str] = "recoverable"
```

実際の身元は `stage + failure_kind + outcome_code` で読む。

例:

```text
stage=curation
failure_kind=terminal_drop
outcome_code=ai_provider_input_rejected
```

### 3. code / outcome_code は具体原因を表す

error class の `code` は具体原因の安定 slug。DB ではこれを
`pipeline_events.outcome_code` に焼く。過去互換用の旧 code 列は持たない。

- stage 固有 leaf error は class または constructor で固定する
- provider 由来 error は ACL mapper が provider error の `CODE` を引き継ぐ
- DB 由来 error は `classify_db_error()` adapter が `code` を返す

`error_class` は実行時の FQN を残す forensic 情報であり、主契約にはしない。

### 4. retryability は派生軸として marker が持つ

retry 可否は重要だが、「どんな失敗か」そのものではない。
stage 横断の運用・集計に必要な派生軸として `retryability` に分離する。

```python
class CurationTerminalDropError(CurationError):
    FAILURE_KIND: ClassVar[str] = "terminal_drop"
    RETRYABILITY: ClassVar[str] = "non_retryable"


class CurationRecoverableError(CurationError):
    FAILURE_KIND: ClassVar[str] = "recoverable"
    RETRYABILITY: ClassVar[str] = "retryable"
```

値:

| 値 | 意味 |
|---|---|
| `retryable` | 同一入力の将来再実行で成功する可能性がある |
| `non_retryable` | 同一入力の単純再試行では回復しない |
| `unknown` | catch-all / adapter 未分類 |

retry 上限に到達したかどうかは intrinsic な性質ではないため、
引き続き payload の `retry_exhausted` などに持つ。

### 5. failure_action は必要な副作用だけを表す

`failure_action` は「失敗の身元」ではなく、handler が取る業務上の処置。
そのため nullable とし、意味の強い処置だけを入れる。

```python
class CurationTerminalDropError(CurationError):
    FAILURE_KIND: ClassVar[str] = "terminal_drop"
    RETRYABILITY: ClassVar[str] = "non_retryable"
    FAILURE_ACTION: ClassVar[str | None] = "drop_article"


class CurationTerminalKeepError(CurationError):
    FAILURE_KIND: ClassVar[str] = "terminal_keep"
    RETRYABILITY: ClassVar[str] = "non_retryable"
    FAILURE_ACTION: ClassVar[str | None] = None
```

`keep_article` / `keep_curation` は原則として action にしない。
多くの場合は「削除しない」という消極的事実であり、`stage + failure_kind` から読める。
将来、明確な副作用として保存・退避・無効化などが発生する場合にのみ値を増やす。

初期値:

| 値 | 意味 |
|---|---|
| `drop_article` | article を削除する |
| `NULL` | 監査対象として明示する副作用なし |

### 6. event_type は error class に持たせない

`failed` / `rejected` / `skipped` / `succeeded` は audit event の種別であり、
例外そのものの性質ではない。

同じ error class でも、どの audit method で記録するかは handler / repository の責務。

### 7. attempt は top-level に持たない

`attempt` は全 stage 共通の意味を持たないため `pipeline_events` の top-level
列にはしない。

- taskiq の `retry_count + 1` は retry 制御用の実行時情報であり、監査契約にはしない
- acquisition の単発 `attempt=1` のような fake 値は焼かない
- completion の `incomplete_articles.attempt_count` だけは業務制御
  (retry budget / stale worker guard) の一部なので
  `CompletionPayload.attempt_count` に snapshot として保存する
- retry 上限に到達した事実は従来どおり payload の `retry_exhausted` で表す

## 投影先

現在の `pipeline_events` では、旧 top-level 3 列の責務を次の属性へ分解している。

| 属性 | 由来 | nullable | 役割 |
|---|---|---:|---|
| `stage` | audit repository / stage root | no | どの段階か |
| `event_type` | audit repository | no | 成功・失敗・棄却・skip |
| `outcome_code` | leaf error / provider mapper / DB adapter / repository | no | 具体原因・結果種別 |
| `retryability` | stage marker / adapter | yes | retry 可否の横断集計 |
| `error_class` | runtime exception | yes | forensic 用 FQN |
| `payload.failure_kind` | stage marker / adapter | yes | stage 内の失敗判断 |
| `payload.failure_action` | stage marker / adapter | yes | 明示的な業務副作用 |
| `payload.attempt_count` | completion Ready | yes | completion の claim 試行番号 snapshot |

成功イベントでは `failure_*` 系属性は `NULL` とし、`event_type='succeeded'` を正本にする。
completion は成功 / skipped / rejected / failed の全 Ready 由来 row で
`payload.attempt_count` を持つ。それ以外の stage には attempt 系 payload を追加しない。

## stage marker 例

| stage | class | failure_kind | retryability | failure_action |
|---|---|---|---|---|
| curation | `CurationTerminalDropError` | `terminal_drop` | `non_retryable` | `drop_article` |
| curation | `CurationTerminalKeepError` | `terminal_keep` | `non_retryable` | `NULL` |
| curation | `CurationRecoverableError` | `recoverable` | `retryable` | `NULL` |
| assessment | `AssessmentTerminalStageBlockedError` | `terminal_stage_blocked` | `non_retryable` | `NULL` |
| assessment | `AssessmentTerminalTargetRejectedError` | `terminal_target_rejected` | `non_retryable` | `NULL` |
| assessment | `AssessmentCategoryMissingError` | `terminal_classification_unresolved` | `non_retryable` | `NULL` |
| assessment | `AssessmentRecoverableError` | `recoverable` | `retryable` | `NULL` |
| embedding | `EmbeddingTerminalStageBlockedError` | `terminal_stage_blocked` | `non_retryable` | `NULL` |
| embedding | `EmbeddingTerminalTargetRejectedError` | `terminal_target_rejected` | `non_retryable` | `NULL` |
| embedding | `EmbeddingRecoverableError` | `recoverable` | `retryable` | `NULL` |
| briefing | `BriefingConfigurationError` | `configuration` | `non_retryable` | `NULL` |

## 外部例外

外部例外は class attr を持てないため、adapter が同じ projection を返す。

```python
@dataclass(frozen=True, slots=True)
class FailureProjection:
    failure_kind: str
    retryability: str
    failure_action: str | None
    code: str  # pipeline_events.outcome_code に焼く値
```

例:

| 外部例外 | stage 文脈 | failure_kind | retryability | failure_action | outcome_code |
|---|---|---|---|---|---|
| `OperationalError` | 任意 | `db_runtime` | `retryable` | `NULL` | `db_runtime_error` |
| `IntegrityError` | curation | `db_constraint` | `non_retryable` | `NULL` | `db_constraint_error` |
| 未分類 `Exception` | 任意 | `unknown` | `unknown` | `NULL` | `unexpected_error` |

外部 DB 例外で article を削除する判断はしない。

## 実装済み状態

1. stage error root に `STAGE` を追加する
2. dispatch marker に `FAILURE_KIND` / `RETRYABILITY` / `FAILURE_ACTION` を追加する
3. audit repository は class attr / adapter から projection する
4. 旧分類 enum と旧大分類列は撤去し、成功は `event_type='succeeded'` で表現する
5. 旧 event code 列は撤去し、`outcome_code` を唯一の event code とする
6. 旧 attempt 列は撤去し、completion のみ `payload.attempt_count` を持つ

## 非目標

- stage 共通 marker class を作らない
- `event_type` を error class に持たせない
- FQN (`error_class`) を主契約にしない
- `keep_article` / `keep_curation` を初期 action 値として増やさない
- completion 以外に `attempt_count` / `retry_attempt` payload を増やさない
