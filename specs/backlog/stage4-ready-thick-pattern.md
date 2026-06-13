# Stage 4 Assessment — 厚い Ready + 下流 Stage 自身が処理開始時に構築 (案 3)

Status: 2026-05-12 完了。Stage 5 で先に確定した案 3 (`specs/backlog/stage5-ready-thick-pattern.md`)
を Stage 4 に横展開した。

## 問題提起 (Stage 4 における旧 Pattern A' の弱点)

Stage 5 で識別した 3 つの問題に加え、Stage 4 では **audit 用 2-hop 逆引き** という
固有の弱点があった。

### 共通の問題 (Stage 5 と同じ)

1. **薄い Ready で構造保証の実体が弱い**: 旧 `ReadyForAssessment(extraction_id,
   translated_title, summary)` は 3 fields。`InScope` のような「型を通った時点で
   内容が確定する」価値が弱い
2. **kiq enqueue → 実行までに DB 状態が変わる**: 上流 Stage 3 task が `try_advance_from`
   で Ready 構築 → kiq → worker pickup → 実処理開始まで時間ずれが生じる
3. **「Service の precondition 分岐を消す」当初目的に対し責務の主語が間違っていた**:
   上流 Stage 3 task が「下流 Stage 4 のための Ready」を構築する設計

### Stage 4 固有の弱点: AuditRepository の 2-hop 逆引き

旧 `AssessmentAuditRepository` は audit row に必要な値を Ready から取れず、
DB 逆引きで補っていた:

- `_article_id_for(extraction_id) -> articles.id`: 1-hop
  (`article_extractions → articles`)
- `_resolve_source_name(extraction_id) -> news_sources.name`: 2-hop
  (`article_extractions → articles → news_sources`)

これは Ready の責務が抜けていた帰結 — Ready 構築時に必要な値を全揃えで保持する
設計にすれば、AuditRepository は逆引きを行う必要がない。Stage 5 にはこの構造的
弱点が無かった (Stage 5 の audit は別経路で処理) ため Stage 4 固有の問題。

## 確定方針 (案 3 適用)

### Ready 型の厚化範囲

```python
class ReadyForAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)
    extraction_id: int = Field(gt=0)
    translated_title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    article_id: int = Field(gt=0)        # audit の article_id 列に詰める
    source_name: str | None              # audit payload; FK 切断時は None
```

判断記録:
- `article_id`: audit `pipeline_events.article_id` 列に詰めるため必須。Ready
  構築時 (1-query) に取得済み
- `source_name`: audit payload `source_name` field。`NewsSource` 不在 / FK 切断時
  耐性のため `str | None` (現状の audit と同挙動を維持)
- `translated_title` / `summary`: 既存どおり assessor 入力 + audit `input_text`
  両方で使用
- `category_slug` 等 in-scope 固有の値は持たない (Ready は処理開始前、AI 呼び出し
  前のため、判定結果側は `call.result` envelope が持つ)

### Repository protocol

cheap exists 2 個 + audit 用 2-hop join + text 取得を **1 query に統合** した
`try_load_for_assessment`:

```sql
SELECT
    ae.translated_title,
    ae.summary,
    ae.article_id,
    ns.name
FROM article_extractions ae
JOIN analyzable_articles a ON a.id = ae.article_id
LEFT JOIN news_sources ns ON ns.id = a.source_id
LEFT JOIN in_scope_assessments isa ON isa.extraction_id = ae.id
LEFT JOIN out_of_scope_assessments oosa ON oosa.extraction_id = ae.id
WHERE ae.id = :extraction_id
  AND isa.id IS NULL
  AND oosa.id IS NULL
LIMIT 1
```

行が無い / 既 in-scope / 既 out-of-scope → `None`。それ以外 → Ready を直接構築
して返す (Repository → Domain Aggregate factory pattern、Stage 5 と同形)。

旧 `exists_in_scope` / `exists_out_of_scope` 2 method は削除。

### taskiq message 型 (新規 `AssessmentTrigger`)

```python
class AssessmentTrigger(BaseModel):
    model_config = ConfigDict(frozen=True)
    extraction_id: int = Field(gt=0)
```

- 配置: `assessment/domain/ready.py` 末尾に同居 (Stage 5 の `EmbeddingTrigger` と対称)
- `feedback_taskiq_basemodel_required.md` 準拠 (taskiq formatter は BaseModel(frozen=True) を要求)
- 既定 `extra='ignore'` により旧 `ReadyForAssessment` 3-fields message を rolling
  deploy 中に新 worker で受け取っても `extraction_id` だけ取り出せる

### maintenance backfill の責務縮退 (選択肢 B)

旧 `article_ids_pending_assessment` (Article ID を返す) → 新
`extraction_ids_pending_assessment` (Extraction ID を返す)。返却列を Article から
ArticleExtraction に変えるだけで、JOIN 構造と age window は同一。

`backfill_assessments` task は新 backlog method で extraction_id を直接取得し、
`AssessmentTrigger` に詰めて kiq するだけのループに縮退:
- `ExtractionRepository.find_by_article_id` / `ReadyForAssessment.try_advance_from`
  への依存を撤去
- `skipped` カウントを廃止 (Stage 4 task の `assess_content_skipped` ログで観測)

### Stage 4 task の処理順序

`assess_content(trigger: AssessmentTrigger, ctx)`:

1. `ReadyForAssessment.try_advance_from(extraction_id, repo)` で Ready 構築 (DB fetch + precondition 検証)
2. Ready が `None` → `assess_content_skipped` ログ + return (rate limit acquire しない)
3. rate limit acquire
4. `AssessmentService.execute(ready, assessor)` で AI 呼び出し + 永続化
5. in-scope 成功 (assessment_id 返却) → `EmbeddingTrigger(analysis_id=...)` で Stage 5 chain
6. out-of-scope / race lost → chain しない

Ready 構築を rate limit より前に置く理由: precondition 未充足 (stale trigger) で
AI quota / Redis rate limit を消費するのを防ぐ (Stage 5 と同方針)。

## 変更後の状態

| 要素 | 旧 (Pattern A') | 新 (案 3) |
|---|---|---|
| `ReadyForAssessment` | 3 fields (extraction_id / translated_title / summary) | 5 fields (+ article_id / source_name) |
| Protocol | `AssessmentExistenceProtocol` (exists 2 method) | `AssessmentPreconditionProtocol` (try_load_for_assessment 1 method) |
| kiq message | `ReadyForAssessment` | `AssessmentTrigger(extraction_id)` |
| Ready 構築タイミング | 上流 Stage 3 task / maintenance | 下流 Stage 4 task (処理開始時) |
| Repository method | `exists_in_scope` + `exists_out_of_scope` (2 query) | `try_load_for_assessment` (1-query atomic) |
| AuditRepository | `_article_id_for` + `_resolve_source_name` (DB 逆引き) | Ready から直接読む |
| maintenance backlog | `article_ids_pending_assessment` (AnalyzableArticleRecord.id) | `extraction_ids_pending_assessment` (ArticleExtraction.id) |
| maintenance task | `try_advance_from` + kiq(ready) | kiq(trigger) のみ |

## Rolling deploy 互換性

DB schema 影響なし。`pipeline_events` payload shape も不変
(`source_name` / `extraction_id` / `article_id` 等の出方は同一)。

taskiq queue の rolling deploy 中:
- **新 worker が旧 message 受信**: Pydantic 既定 `extra='ignore'` で
  `AssessmentTrigger.model_validate({...3 fields...})` は `extraction_id` だけ
  取り出して成功。下流処理は Ready 構築時に最新の DB 状態を反映するため正常進行
- **旧 worker が新 message 受信**: 旧 `ReadyForAssessment` は
  `translated_title` / `summary` 必須なので validate 失敗 → taskiq retry で
  新 worker に拾われる (rolling deploy 中の数秒のみの過渡状態)

Stage 5 deploy と同じ手順 (assess_content queue が自然 drain するタイミングで
deploy するのが安全) で運用可能。

## 関連 memory

- `project_typed_pipeline_preconditions.md` (2026-05-11 確定版) — 案 3 の根拠
- `feedback_bc_boundary_guarantees_downstream.md` — base 原則 (BC 境界が下流の
  信頼性を保証する) は維持。Stage 4 の Ready 厚化はその具体実装
- `feedback_taskiq_basemodel_required.md` — taskiq kiq 引数は BaseModel(frozen=True) 必須
- `feedback_failure_visibility.md` — race lost / RuntimeError fail-fast の方針維持

## 関連 spec

- `specs/backlog/stage5-ready-thick-pattern.md` — Stage 5 で先に実装した案 3 の rationale doc
