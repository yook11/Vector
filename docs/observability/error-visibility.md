# Error Visibility (工程別エラー可視化)

アサート失敗・未知例外が起きたとき、Logfire 上で **(1) どの工程 (stage) で・
(2) どの場所 (source / article_id / op) で・(3) どんなエラー (type / failure_kind /
level) だったか** を即座に絞り込めるようにするための設計と実装単位。

検証環境: `logfire >= 4.34.0` (`backend/pyproject.toml`)。

## Problem

未知例外 (想定外 `Exception`) と不変条件違反が、現状は「どの工程で起きたか」を
Logfire 上で工程軸に集計・絞り込みできない。stacktrace は届くが、`stage` 属性が
無いため「assessment 工程の未知エラーだけ」「dispatch だけ」を素早く切り出せない。

## Evidence (現状)

| 領域 | 実装 | 参照 |
|---|---|---|
| 初期化 | `setup_logfire(service_name)` が `logfire.configure(send_to_logfire="if-token-present", console=False, environment=...)`。`StructlogProcessor` で structlog ログも Logfire に集約 | `app/logfire/setup.py:24` |
| service 分離 | `vector-api` / `vector-worker-<label>` / `vector-scheduler` | `app/logfire/setup.py` ほか |
| 明示 span | AI 分析 3 工程のみ `article_stage` span (属性 `stage` / `task_name` / `result` / `article_id`)。taskiq `OpenTelemetryMiddleware` の `execute/<task_name>` span の子 | `app/logfire/article_stage.py:115` / `app/queue/tasks/{curation,assessment,embedding}.py` |
| task span 親 | 各 broker の `OpenTelemetryMiddleware` を先頭配線 | `app/queue/brokers.py:72` |
| 失敗属性投影 | `project_failure(exc) -> FailureProjection(failure_kind, retryability, failure_action, code, stage, failure_reason)`。marker → DB → catch-all (`failure_kind="unknown"` / `code="unexpected_error"`) | `app/audit/failure_projection.py:74` |
| error FQN | `exception_fqn(exc)` → `module.qualname` | `app/audit/error_fields.py:14` |
| 監査 SSoT | `pipeline_events` 行 (`stage` / `outcome_code` / `retryability` / `error_class` / payload) | `docs/observability/pipeline-events-failure-attributes.md` |
| 工程語彙 SSoT | `Stage` StrEnum 11 値 (`dispatch` `acquisition` `completion` `curation` `assessment` `embedding` `backfill_curate` `backfill_assess` `backfill_embed` `briefing` `trend_discovery`)。DB CHECK `ck_pipeline_events_stage` と一致 | `app/audit/domain/event.py:12` |
| 既存 metric | `vector.curation.processing_outcome` / `vector.assessment.processing_outcome` (属性 `result` のみ、ID 非搭載)。service / handler 境界で emit | `app/analysis/curation/metrics.py:19` |
| PII 防御 | `VectorDomainError.__str__` を class 名 + `SAFE_ATTRS` に固定、`redact_secrets` / 2000 字 cap | `app/logfire/exceptions.py:12` / `app/audit/error_fields.py` |

### 2 つの穴

1. **FastAPI の未知 500 が工程軸に乗らない。** `app/main.py:184` は `NotFoundError` /
   `DuplicateError` / `InvalidQueryError` の 3 ハンドラのみで、汎用 `Exception`
   ハンドラが無い。router 内の想定外例外は default 500 になり、`stage` 属性が無いため
   「どの API 工程で落ちたか」を Logfire で集計できない。
2. **audit-write 失敗に集約 metric が無い。** `*_audit_dropped` の `logger.exception`
   は約 25 箇所 (`app/collection/.../failure_handling.py` / `app/analysis/.../failure_handling.py` /
   `app/audit/stages/trend_discovery.py` / `app/queue/tasks/*.py`)。`StructlogProcessor`
   経由でログとしては Logfire に届くが、**rate を集計・アラートできる counter が無い**ため
   監査パイプラインの恒常失敗を検知できない。

### アサートの実態

production の `assert` は `app/analysis/curation/cli/recuration_service.py:282` の 1 箇所のみ。
Vector で「不変条件違反」を表すのは実質 `VectorDomainError` 系 marker と catch-all の
`unknown` / `unexpected_error` 投影。本 spec は「アサート失敗」を広義 (`AssertionError`
+ 想定外例外) として扱う (厳密化は[未決](#未決事項)参照)。

## 設計 — 3 軸を属性に落とす

公式指針: span_name / message template は低カーディナリティに固定し、識別子は属性
(JSON 列) に分離する。高カーディナリティ ID は span 属性には載せてよいが metric label
には載せない。

| 軸 | 属性 | カーディナリティ | 規約 |
|---|---|---|---|
| 工程 | `stage` | 低 (11 値) | `Stage` enum を唯一の語彙にする。Logfire 専用の stage 値を作らない |
| 場所 | `op` | 低 | 操作名 (task 名 / 関数名)。span_name は固定 |
| | `source_id` | 中 | 集計ディメンションに使える |
| | `article_id` / `curation_id` 等 | **高** | span 属性には載せる (個票 trace 特定)。**metric label には載せない** |
| 種別 | `exception.type` / `.message` / `.stacktrace` | — | span 内貫通で OTel が自動記録 |
| | `failure_kind` / `code` / `retryability` | 低 | `project_failure(exc)` の投影を span 属性に複写し、`pipeline_events` と同語彙で串刺し |
| | `error_class` | 高 | `exception_fqn(exc)` の FQN (forensic) |
| | `level` | — | 重大度軸 (`trace`..`fatal` の 7 段) |

### span 階層

```
[taskiq OpenTelemetryMiddleware] execute/<task_name>   ← OTel 自動 (各 broker)
  └─ pipeline_stage span (stage=acquisition, op=..., source_id=...)   ← 本 spec で追加
       └─ instrument_sqlalchemy / instrument_httpx の子 span 群     ← 既存
```

worker は「task span の子に stage span を 1 つ」。

API 側は機構によって出力が変わる点に注意 ([未決](#未決事項) #1 と連動)。

- **middleware / dependency / route wrapper** で route 実行を span で囲めば、
  `instrument_fastapi` の server span の子 span として属性が乗る (worker と同じ span 軸)。
- **汎用 `Exception` ハンドラ + `logfire.log(...)`** は span ではなく **log record** を出す。
  さらに handler が例外を捕捉して `JSONResponse` を返すと「handled」扱いになり、server
  span 側に exception event が自動記録されない場合がある。属性は log record に乗るため、
  クエリ・検証は span ではなく `records` の log attributes を見る。

### Logfire API の正しい使い分け (★4.34.0)

| 状況 | 書き方 | 効果 |
|---|---|---|
| span 内で例外を貫通 | 何もしない (span の外へ raise) | 自動記録 + **span level 自動 `error`** |
| 例外を握りつぶすが残す | `span.set_level("error")` + `span.record_exception(exc)` | `record_exception` 単独は **level を上げない**ため `set_level` 併用必須 |
| log として error 記録 | `logfire.exception("msg", **attrs)` | level=`error` + 現在の traceback |
| fatal (不変条件違反) | `logfire.fatal(...)` / `logfire.log(level="fatal", msg_template=..., attributes=...)`。span level を上げるなら `logfire.span(name, _level="fatal", ...)` | level=`fatal` |

> **禁止:** `logfire.exception(..., _level=...)` / `logfire.error(..., _level=...)`。
> `_level` 引数は `logfire.span` のみが持つ。`logfire.exception` / `logfire.error` に
> 渡すと level 上書きにならず `_level` という名の通常属性として記録される。

## Invariants

- **I1**: 工程語彙は `Stage` enum (11 値) を唯一の SSoT とする。Logfire 専用の stage
  値を作らない。helper は `stage` を `Stage` 型で受け、任意 str を受けない。
- **I2**: span_name は低カーディナリティ固定 (`pipeline_stage` / `article_stage`)。
  識別子 (`article_id` 等) は属性に分離する。
- **I3**: 高カーディナリティ ID は span 属性には載せてよいが、metric label には載せない
  (既存 `processing_outcome` が `result` のみを label にする規律を踏襲)。
- **I4**: 例外メッセージの PII 防御は `VectorDomainError.__str__` 固定 + `redact_secrets`
  に依存する。Logfire の scrubbing には依存しない (scrubbing は属性値のみ対象で
  `exception.message` を scrub しない)。新規 span / log に本文・prompt・AI response・
  URL query・認証情報を載せない。
- **I5**: span 内例外は貫通させて自動記録する (level=error 自動)。握りつぶす場合のみ
  `span.set_level` + `record_exception` を明示併用する。`record_exception` 単独は禁止。
- **I6**: metric emit は永続化 / 決定境界の所有者が出す (既存 metric と同じ規律)。
- **I7**: span 計装は監査 (`pipeline_events`) を置き換えない。`pipeline_events` が
  SSoT、Logfire span は補助 telemetry 層。

## 実装単位

### 共通ヘルパ (`app/logfire/stage_span.py`, 新規)

`article_stage.py` の「ステージごとに独立記録口」方針は AI 3 工程の result 語彙の
差異に由来する。非 AI 工程は result 語彙を持たないため、`Stage` を強制注入する薄い
context manager 1 本に統一する。

PR1 で span 化する未計装 worker 工程は、単発 run の **7 タスク**。AI 3 工程
(`curation` / `assessment` / `embedding`) は既に `article_stage` span を持つため対象外。
`retention.py` は `Stage` 値を持たない (パイプライン工程でない) ため対象外。

| Stage | task entrypoint | ファイル |
|---|---|---|
| `acquisition` | `acquire_source` (source 取得) | `app/queue/tasks/acquisition.py` |
| `completion` | `scrape_html_body` (本文補完) | `app/queue/tasks/completion.py` |
| `briefing` | `generate_briefing_for_category` (1 カテゴリ生成) | `app/queue/tasks/briefing.py` |
| `trend_discovery` | `run_trend_discovery` | `app/queue/tasks/trend_discovery.py` |
| `backfill_curate` | `backfill_curations` | `app/queue/tasks/backfill.py:427` |
| `backfill_assess` | `backfill_assessments` | `app/queue/tasks/backfill.py:594` |
| `backfill_embed` | `backfill_embeddings` | `app/queue/tasks/backfill.py:755` |

**dispatcher / fan-out は PR1 対象外 (PR1b へ委譲)**: `dispatch_high/medium/low/sources`
(`Stage.DISPATCH`)・`dispatch_weekly_briefings`・`dispatch_html_fetch_jobs` は per-source
event + run metric + middleware の `execute/<task_name>` span で部分観測済みで、run 単位
span の意味付け (fan-out 全体を 1 span にするか) は別 PR で扱う。

```python
from contextlib import contextmanager
from collections.abc import Iterator

import logfire
from logfire import LogfireSpan

from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.audit.failure_projection import project_failure

_SPAN_NAME = "pipeline_stage"  # 低カーディナリティ固定


@contextmanager
def pipeline_stage_span(
    stage: Stage,
    *,
    op: str,
    source_id: int | None = None,
    article_id: int | None = None,
) -> Iterator[LogfireSpan]:
    """非 AI 工程を span で囲む。stage を Stage enum 値で強制注入する。

    span 内 raise は OTel exception event として自動記録され span level を error
    に上げる。失敗種別 (failure_kind / code / error_class) は backstop で span 属性に
    複写し、pipeline_events と同語彙で工程横断クエリできるようにする。
    """
    attrs: dict[str, object] = {"stage": stage.value, "op": op}
    if source_id is not None:
        attrs["source_id"] = source_id
    if article_id is not None:
        attrs["article_id"] = article_id
    with logfire.span(_SPAN_NAME, **attrs) as span:
        try:
            yield span
        except BaseException as exc:  # ← PR2 で追加する failure 属性 backstop
            proj = project_failure(exc)
            span.set_attribute("failure_kind", proj.failure_kind)
            span.set_attribute("code", proj.code)
            span.set_attribute("retryability", proj.retryability.value)
            span.set_attribute("error_class", exception_fqn(exc))
            raise
```

### PR 分割

| PR | 内容 | 対象ファイル | `Stage` 語彙への依存 |
|---|---|---|---|
| **PR1 (Step 0+1)** | Step 0: 本番 Live view で `span_name='article_stage'` の存在を目視確認 (コード変更なし)。Step 1: `pipeline_stage_span` 追加 (failure backstop 抜きの最小形) + 単発 run の未計装 worker **7 タスク** ([上表](#共通ヘルパ-applogfirestage_spanpy-新規)) を span 化。dispatcher は PR1b | `app/logfire/stage_span.py` (新規) / `app/queue/tasks/{acquisition,completion,briefing,trend_discovery,backfill}.py` の task entry | 既存 enum を使うのみ |
| **PR2 (Step 2)** | span に `failure_kind` / `code` / `error_class` (+`retryability`) を複写する backstop を追加。`pipeline_stage_span` と既存 `article_stage` の両方に適用 | `app/logfire/stage_span.py` / `app/logfire/article_stage.py` | 既存 enum を使うのみ |
| **PR3** | `vector.audit.dropped` counter を追加し、`*_audit_dropped` 約 25 箇所で `record_audit_dropped(stage)` を呼ぶ。穴 2 を埋める | `app/audit/metrics.py` (新規) + 各 `failure_handling.py` / `app/queue/tasks/*.py` / `app/audit/stages/trend_discovery.py` | 既存 enum を使うのみ |
| **PR4 (要合意後)** | API 未知例外に工程文脈を付け穴 1 を埋める。機構は未決 (a) 汎用 `Exception` ハンドラ + `logfire.log` (= log record) / (b) middleware・route wrapper (= span)。実装先は機構による (`app/exception_handlers.py` か middleware) | `app/main.py:184` に登録 | **`surface` 語彙 + 機構の合意が前提** ([未決](#未決事項) #1) |

PR1〜3 は `Stage` enum に一切触れず、DB schema / API shape も変えないため独立に進められる。
PR4 のみ語彙設計を含むため後段に置く。

#### PR3 helper

```python
# app/audit/metrics.py (新規)
import logfire

from app.audit.domain.event import Stage

_audit_dropped_counter = logfire.metric_counter(
    "vector.audit.dropped",
    unit="1",
    description="監査書き込み失敗 (pipeline_events append 失敗) の件数。stage 別。",
)


def record_audit_dropped(stage: Stage) -> None:
    """監査書き込み失敗を 1 件記録する。stage のみ載せ、ID は載せない (I3)。"""
    _audit_dropped_counter.add(1, attributes={"stage": stage.value})
```

各 `*_audit_dropped` の `logger.exception(...)` に隣接して `record_audit_dropped(Stage.X)`
を呼ぶ。ログ (forensic) と metric (集計・アラート) を併設し、ログは触らない。

## 可視化

クエリ対象は Logfire の `records` テーブル
(`span_name` / `attributes`(JSON) / `level` / `is_exception` / `exception_type` /
`exception_message` / `service_name` / `start_timestamp`)。

### Explore

```sql
-- 工程別エラー件数 (種別内訳)
SELECT attributes->>'stage' AS stage, exception_type,
       attributes->>'failure_kind' AS failure_kind, count(*) AS errors
FROM records WHERE is_exception
  AND start_timestamp > now() - interval '24 hours'
GROUP BY stage, exception_type, failure_kind ORDER BY errors DESC;
```

```sql
-- 最近のエラー一覧 (工程 / 場所 / 種別 列)
SELECT start_timestamp, attributes->>'stage' AS stage,
       attributes->>'source_id' AS source_id, attributes->>'article_id' AS article_id,
       exception_type, exception_message, service_name
FROM records WHERE is_exception ORDER BY start_timestamp DESC LIMIT 100;
```

`attributes->>'key'` の GROUP BY / `level` の順序比較は外部 SaaS 側スキーマ依存のため、
**本番 Explore で実クエリ検証してから** Dashboard / Alert に固定する (要確認)。

### Dashboard / Alerts

- 工程別エラー率の時系列 (`time_bucket($resolution, start_timestamp)` で系列化)。
  標準 Dashboard の **Exceptions** テンプレを複製し `attributes->>'stage'` で系列分割。
- Alerts: `records` への定期 SQL を `is_exception` / `level` / `attributes->>'stage'` で
  絞り Slack へ。例: assessment 工程の `failure_kind='unknown'` 急増 (DeepSeek bad JSON
  監視) / `vector.audit.dropped` の急増。
- **Issues** (例外を fingerprint で自動グルーピング + Slack 通知) を新規エラー検知の中核にする。

## 検証

`send_to_logfire="if-token-present"` のため **dev / CI / test では logfire は完全 no-op
(外部送信なし)**。よって主検証は capfire (`tests/logfire/`) で span 属性 / metric を
assert する。`get_collected_metrics` は zero-metric で crash する等の作法は
[capfire メモ参照]。Step 0 のみ本番 Live view 目視。

| PR | 検証 |
|---|---|
| PR1 | capfire で `pipeline_stage` span が出て `stage` 属性が `Stage` 値であることを assert (backfill 含む 7 タスク)。貫通例外で exception event が乗り span level が error (logfire level_num=17) へ昇格することを assert (I5)。Step 0 は本番 Live view 目視 |
| PR2 | 未知例外を投げ、span に `failure_kind='unknown'` / `code='unexpected_error'` / `error_class` が乗ることを capfire で assert。**`article_stage` の `_ALLOWED_DOMAIN_KEYS` ([test_article_stage.py:33](../../backend/tests/logfire/test_article_stage.py)) に failure 属性 4 キーを追加**し、追加後も本文 / URL / prompt / AI response が span に乗らない anti-test (allowlist 超過キー非含有) が緑であることを確認 (I4) |
| PR3 | audit session を故意に壊し `vector.audit.dropped` が +1、属性が `stage` のみであることを capfire で assert |
| PR4 | 機構 (a) なら router で未知例外 → 500 + **`records` の log attributes** に `stage`/`surface`/`failure_kind` 確認。機構 (b) なら server span 子 span の **span 属性**確認 (語彙 + 機構合意後) |

## Non-goals

- `pipeline_events` のスキーマ・契約・`outcome_code` 語彙を変えない。
- API response shape を変えない。
- `Stage` enum を PR1〜3 では変えない (PR4 の `surface` 合意まで凍結)。
- distributed trace 連結 (enqueue→worker) は `default_retry_count=0` の現状では対象外。
  retry 有効化時に別 spec で `get_context` / `attach_context` を配線する。
- sampling 設定変更は本 spec では扱わない (将来コスト最適化時に error / fatal の保全を別途)。
- dispatcher / fan-out (`dispatch_*` / `dispatch_weekly_briefings` /
  `dispatch_html_fetch_jobs`) の run 単位 span は PR1 では扱わない (**PR1b** へ委譲)。

## 未決事項

1. **API 工程の `surface` 語彙 + 記録機構 (PR4 の前提)**: `Stage` enum に `api` 値は
   無く、SSoT。FastAPI 未知例外に工程を付けるには弁別子と記録機構の両方を決める。
   - 語彙: A案=`Stage` enum に `api` を追加 (DB CHECK / `pipeline_events` と整合が必要) /
     **B案 (推奨)**=最上位弁別子 `surface` (`pipeline` / `api`) を導入し `stage` は
     `surface="pipeline"` のときだけ載せる。pipeline 工程でないものを `stage` に混ぜず
     既存不変条件 (I1 / DB CHECK) を壊さない。
   - 機構: (a) 汎用 `Exception` ハンドラ + `logfire.log(...)` → **log record** に属性が
     乗る (簡単。span にはならない) / (b) middleware・dependency・route wrapper → route
     実行を span で囲み worker と **同じ span 軸**に統一できる (handler より配線が要る)。
     検証対象 (log attributes か span 属性か) は機構で決まる ([span 階層](#span-階層)参照)。
2. **fatal 昇格の境界 (未知例外と不変条件違反を分ける)**: 未知例外を一律 fatal にすると
   `failure_kind="unknown"` と `error_kind="invariant_violation"` の境界が崩れる。
   - 未知例外 (`failure_kind="unknown"`): `level=error` のまま。
   - `AssertionError` または明示的 invariant marker のみ: `level=fatal` +
     `error_kind="invariant_violation"`。
   昇格させる場合 PR2 で `logfire.span(_level="fatal")` を足す (`logfire.exception` の
   `_level` は無効。[Logfire API](#logfire-api-の正しい使い分け-434)参照)。
3. **retry 有効化時の trace 連結**: 上記 Non-goals 参照。

## 参照

- `docs/observability/pipeline-events-design.md`
- `docs/observability/pipeline-events-failure-attributes.md`
- `docs/observability/memory-monitoring.md`
