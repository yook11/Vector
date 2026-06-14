# Tier 2 - Embedding dimension SSoT and audit payload naming

**Status: DRAFT (implementation-ready proposal)**

Tier 2 は pipeline stage naming roadmap の `embedding-04` と `embedding-03` を扱う。
主目的は Stage 5 document embedding の次元数契約を `EMBEDDING_DIMENSION` に結線し、
副目的として embedding audit payload の model 識別子を他 AI stage と同じ
`ai_model` へ寄せること。

## Problem

Stage 5 document embedding の次元数 `768` が、現状は複数箇所の literal / private
constant として独立している。

```text
EMBEDDING_DIMENSION                           (VO / 永続化契約)
GEMINI_EMBEDDING_SPEC.dimension               (embedder が宣言する契約次元)
GEMINI_EMBEDDING_SPEC.output_dimensionality   (Gemini SDK request config)
AnalyzedArticleRecord.embedding HALFVEC(768)  (DB column type)
trend_discovery repository の HALFVEC cast 用 768
```

そのため、どれか 1 箇所だけが変わっても既存 test が緑のまま残り、
実行時に `EmbeddingVector` の長さ検証や DB cast で破綻する余地がある。

また、pipeline_events audit payload の AI model 識別子は curation / assessment /
briefing が `ai_model` を使う一方、embedding だけ `embedding_model` を使っている。
これは同じ「AI model 識別子」に対する stage 間の語彙揺れである。

## Evidence

- `backend/app/analysis/embedding/domain/value_objects.py`
  - `EMBEDDING_DIMENSION = 768`
  - `EmbeddingVector` が `len(v) == EMBEDDING_DIMENSION` を検証する。
- `backend/app/analysis/embedding/ai/spec.py`
  - `_GEMINI_DIMENSION = 768`
  - `GEMINI_EMBEDDING_SPEC.dimension` と `output_dimensionality` の両方に使っている。
- `backend/app/analysis/embedding/ai/gemini.py`
  - `output_dimensionality=self.SPEC.output_dimensionality` を
    `EmbedContentConfig` へ渡している。
- `backend/app/models/analyzed_article_record.py`
  - `embedding = mapped_column(HALFVEC(768))`
  - 実行確認済み: `AnalyzedArticleRecord.__table__.c.embedding.type.dim == 768`
- `backend/app/insights/trend_discovery/repository.py`
  - `_EMBEDDING_DIM = 768`
  - raw SQL の `.columns(embedding=HALFVEC(_EMBEDDING_DIM))` に使用している。
- `backend/app/audit/domain/payloads.py`
  - `EmbeddingPayload.embedding_model`
- `backend/app/audit/stages/embedding.py`
  - success audit で `embedding_model=embedder.model_name`
  - `vector_dimension=embedder.dimension`
- `backend/app/queue/tasks/embedding.py`
  - structured log `embedding_ai_rate_limit_gate_skipped` は
    `embedding_model=embedder.model_name` を出す。
- app 側 reader
  - `pipeline_events.payload` を `EmbeddingPayload` / `PipelineEventPayload` に
    Pydantic で読み戻す production reader は現状ない。
  - `pipeline_health` / `source_health` は payload 詳細を読まない。

## Decisions

1. `EMBEDDING_DIMENSION` を Stage 5 document embedding の VO / 永続化契約の正本にする。

2. `GEMINI_EMBEDDING_SPEC.dimension` と
   `GEMINI_EMBEDDING_SPEC.output_dimensionality` はどちらも
   `EMBEDDING_DIMENSION` を参照する。

3. `dimension` と `output_dimensionality` は統合しない。
   - `dimension`: Vector 内部の VO / DB / 永続化契約値。
   - `output_dimensionality`: Gemini SDK に渡す外部 API config。
   - 責務は別だが、現運用では必ず `EMBEDDING_DIMENSION` と一致させる。

4. ORM model の `HALFVEC(768)` は literal のまま維持する。
   - `app.models` から `app.analysis.embedding.domain` へ production dependency を
     増やさない。
   - 一致は invariant test で保証する。

5. `trend_discovery` の `_EMBEDDING_DIM = 768` も production literal のまま維持する。
   - insights BC から analysis embedding domain への production import は増やさない。
   - 同じ物理カラム契約を使うため、invariant test で `EMBEDDING_DIMENSION` と
     一致保証する。

6. 新規 `pipeline_events.payload` の Stage 5 model field は `ai_model` にする。

7. 過去の `pipeline_events.payload.embedding_model` は rewrite しない。

8. 旧 payload を読む互換処理は今は追加しない。
   - 現アプリ内に `payload.embedding_model` の production reader がないため。
   - 将来 reader / dashboard / SQL を追加する場合は、必要に応じて
     `COALESCE(payload->>'ai_model', payload->>'embedding_model')` 相当を検討する。

9. structured log の `embedding_ai_rate_limit_gate_skipped.embedding_model` は今回変えない。
   - audit payload 契約とは別スキーマであり、意味も伝わる。
   - ログ field rename は Tier 2 の non-goal とする。

## Invariants

- `EMBEDDING_DIMENSION` は Stage 5 document embedding の VO / 永続化契約値である。
- Search BC の query embedding は別 BC であり、この SSoT の対象外である。
- `EmbeddingVector` は常に `EMBEDDING_DIMENSION` 次元のみ受け入れる。
- `GEMINI_EMBEDDING_SPEC.dimension == EMBEDDING_DIMENSION`
- `GEMINI_EMBEDDING_SPEC.output_dimensionality == EMBEDDING_DIMENSION`
- `AnalyzedArticleRecord.__table__.c.embedding.type.dim == EMBEDDING_DIMENSION`
- `trend_discovery` の HALFVEC cast dimension は `EMBEDDING_DIMENSION` と一致する。
- `dimension` と `output_dimensionality` は field として分ける。
- 新規 embedding audit payload は `ai_model` を使う。
- `vector_dimension` は embedder が宣言する契約次元の audit snapshot である。
  API 応答 vector の実測 `len()` ではない。実測長は `EmbeddingVector` が別途検証する。
- `vector_dimension` は rename しない。

## Non-goals

- embedding 次元数そのものの変更。
- DB schema migration。
- `HALFVEC(768)` の型変更。
- ORM model から analysis embedding domain への production dependency 追加。
- insights BC から analysis embedding domain への production dependency 追加。
- 過去 `pipeline_events.payload` の rewrite。
- 旧 `payload.embedding_model` を読む production 互換 reader の追加。
- public API / frontend schema の変更。
- Search / query embedding への拡大。
- structured log field `embedding_model` の rename。
- `vector_dimension` の rename。

## Implementation Scope

### 1. Dimension SSoT

`backend/app/analysis/embedding/ai/spec.py` で
`GEMINI_EMBEDDING_SPEC.dimension` / `output_dimensionality` が
`EMBEDDING_DIMENSION` を参照するようにする。

期待形:

```python
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION

GEMINI_EMBEDDING_SPEC = EmbeddingCallSpec(
    provider=_GEMINI_PROVIDER,
    model=_GEMINI_MODEL,
    dimension=EMBEDDING_DIMENSION,
    output_dimensionality=EMBEDDING_DIMENSION,
    task_type="RETRIEVAL_DOCUMENT",
    document_prefix="",
    rate_limit_policy=AIModelRateLimitPolicy(...),
)
```

`_GEMINI_DIMENSION` は削除する。

### 2. ORM literal guard

`backend/app/models/analyzed_article_record.py` の `HALFVEC(768)` は変更しない。

代わりに invariant test で次を必ず検証する。

```python
assert (
    AnalyzedArticleRecord.__table__.c.embedding.type.dim
    == EMBEDDING_DIMENSION
)
```

### 3. Insights HALFVEC cast guard

`backend/app/insights/trend_discovery/repository.py` の `_EMBEDDING_DIM = 768` は変更しない。

代わりに invariant test で `_EMBEDDING_DIM == EMBEDDING_DIMENSION` を検証する。
private constant 参照を避ける場合は、当該 repository が構築する `HALFVEC` cast
dimension を検証できる形で guard する。

### 4. Audit payload naming

`EmbeddingPayload` を変更する。

Before:

```python
embedding_model: str | None = None
vector_dimension: int | None = None
```

After:

```python
ai_model: str | None = None
vector_dimension: int | None = None
```

`EmbeddingAuditRepository.append_success` は `ai_model=embedder.model_name` を焼く。
失敗 payload 側の `embedding_model=None` は削除するか、必要なら `ai_model=None` に
置換する。

### 5. Stale docs

`backend/app/analysis/embedding/domain/value_objects.py` の stale docstring を更新する。

- `Stage 3` ではなく `Stage 5 document embedding` と書く。
- 削除済みの `DIMENSION ClassVar` ではなく `GEMINI_EMBEDDING_SPEC.dimension` と
  一致する前提を書く。
- Search BC の query embedding は対象外であることを明記する。

## Required Tests

以下は optional ではなく Done を支える必須 guard とする。

```text
GEMINI_EMBEDDING_SPEC.dimension == EMBEDDING_DIMENSION
GEMINI_EMBEDDING_SPEC.output_dimensionality == EMBEDDING_DIMENSION
AnalyzedArticleRecord.__table__.c.embedding.type.dim == EMBEDDING_DIMENSION
trend_discovery の HALFVEC cast dimension == EMBEDDING_DIMENSION
EmbeddingPayload(...).model_dump(...) contains ai_model
EmbeddingPayload(...).model_dump(...) does not contain embedding_model
EmbeddingAuditRepository.append_success writes payload.ai_model
EmbeddingAuditRepository.append_success writes vector_dimension from embedder.dimension
```

既存の literal `768` assert は、可能な限り `EMBEDDING_DIMENSION` 参照へ置換する。
fixture / historical migration / explanatory comment の literal `768` は残してよい。

grep guard:

```bash
rg -n "embedding_model" backend/app backend/tests
```

許可する残存:

- `backend/app/queue/tasks/embedding.py` の structured log field。
- その structured log を検証する tests。
- historical docs / specs / migrations の旧名説明。

## Deploy / Operation

- queue DTO の wire key 変更ではない。
- DB schema 変更ではない。
- public API response shape 変更ではない。
- stop-the-world deploy / queue drain は不要。
- rolling deploy 中、新旧 worker により `pipeline_events.payload` に
  `embedding_model` と `ai_model` が一時混在しうる。
- payload reader が現状ないため、アプリ動作上の互換 reader は追加しない。

## Done

- `EMBEDDING_DIMENSION` が Stage 5 document embedding の VO / 永続化契約 SSoT に
  なっている。
- `GEMINI_EMBEDDING_SPEC.dimension` が `EMBEDDING_DIMENSION` を参照している。
- `GEMINI_EMBEDDING_SPEC.output_dimensionality` が `EMBEDDING_DIMENSION` を参照している。
- ORM model の `HALFVEC(768)` と `EMBEDDING_DIMENSION` の一致が test で保証されている。
- `trend_discovery` の HALFVEC cast dimension と `EMBEDDING_DIMENSION` の一致が
  test で保証されている。
- 新規 embedding audit payload が `ai_model` を出す。
- 新規 embedding audit payload が `embedding_model` を出さない。
- `vector_dimension` が embedder 契約次元として記録されることが test で保証されている。
- app/test の意図しない `embedding_model` 残存がない。
- stale docstring が現在の構造と一致している。
- `ruff check` / `ruff format --check` / 対象 tests が通る。
