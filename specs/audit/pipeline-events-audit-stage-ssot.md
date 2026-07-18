# pipeline_events audit stage SSoT

Status: Implemented (PR #840)
日付: 2026-06-27

本 spec は、audit stage 選択の重複をなくすために合意した方針を記録する。
[`pipeline-events-failure-attribute-projection.md`](./pipeline-events-failure-attribute-projection.md) のうち stage 所有に関する
記述は本 spec で置き換える。失敗属性 projection の契約は、本 spec が明示的に
変更する箇所を除き引き続き有効とする。

## 問題

現状の `pipeline_events.stage` は複数の層で選ばれている。

- audit repository の append site にある `Stage` enum member。
- 失敗経路の `Error.STAGE -> FailureProjection.stage -> projection.stage or Stage.X`。
- drop site の `record_audit_dropped(Stage.X)`。
- `pipeline_stage_span(Stage.X, ...)` と一部の raw string observability label。
- backfill call site の `stage=Stage.BACKFILL_*` と
  `backfill_stage="curate|assess|embed"` の二重指定。

`Stage` 型は Stage でない値を弾けるが、誤った `Stage.X` の取り違えは弾けない。
目的は、audit write path から public な `stage` parameter をなくし、間違った stage
選択を構造的に起きにくくすること。

## 正本

SSoT は 2 種類に分ける。

1. `Stage` enum は DB wire value の SSoT。
   - `pipeline_events.stage` の DB CHECK constraint と一致し続ける必要がある。
   - read filter や span label など、wire value の語彙 member を選ぶだけの箇所では、
     `Stage.X` を直接参照してよい。

2. 各 audit repository は、自分が書く audit event stage の SSoT。
   - mono-stage repository は `STAGE` を公開する。
   - rescue / aged-out event も書く repository は `STAGE` と `BACKFILL_STAGE` を公開する。
   - business method は `stage` を受け取らない。

期待形:

```python
class CurationAuditRepository:
    STAGE: ClassVar[Stage] = Stage.CURATION
    BACKFILL_STAGE: ClassVar[Stage] = Stage.BACKFILL_CURATE

    async def _append_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
        retryability: Retryability | None = None,
    ):
        await self._events.append(
            stage=self.STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
            retryability=retryability,
        )

    async def _append_backfill_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
        retryability: Retryability | None = None,
    ):
        await self._events.append(
            stage=self.BACKFILL_STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
            retryability=retryability,
        )
```

この funnel の目的は DRY ではなく構造的な正しさである。stage は、その stage を
所有する repository 内でだけ注入する。funnel は `**kwargs` を持たず、
`PipelineEventRepository.append()` の `stage` 以外の optional field を明示列挙する。
これにより `_append_event(stage=...)` は API shape 上の誤りとして扱える。

## 決定事項

1. Audit repository の stage constant を event-stage SSoT とする。

   mono-stage repository:

   - `SourceAcquisitionAuditRepository.STAGE = Stage.ACQUISITION`
   - `ArticleCompletionAuditRepository.STAGE = Stage.COMPLETION`
   - `DispatchAuditRepository.STAGE = Stage.DISPATCH`
   - `BriefingAuditRepository.STAGE = Stage.BRIEFING`
   - `TrendDiscoveryAuditRepository.STAGE = Stage.TREND_DISCOVERY`

   dual-stage repository:

   - `CurationAuditRepository.STAGE = Stage.CURATION`
   - `CurationAuditRepository.BACKFILL_STAGE = Stage.BACKFILL_CURATE`
   - `AssessmentAuditRepository.STAGE = Stage.ASSESSMENT`
   - `AssessmentAuditRepository.BACKFILL_STAGE = Stage.BACKFILL_ASSESS`
   - `EmbeddingAuditRepository.STAGE = Stage.EMBEDDING`
   - `EmbeddingAuditRepository.BACKFILL_STAGE = Stage.BACKFILL_EMBED`

   同じ wire stage を複数 repository が書く場合がある。たとえば
   `Stage.BACKFILL_CURATE` は、curation payload の aged-out event を書く
   `CurationAuditRepository` と、backfill run/item event を書く
   `BackfillAuditRepository` の両方から使われる。これは SSoT の重複ではなく、
   event stage の所有者が「その event を書く repository」であるためである。

2. Repository business method は `PipelineEventRepository.append()` へ直接 `stage` を渡さない。

   `_append_event()` / `_append_backfill_event()` など repository-private な funnel を呼ぶ。
   funnel は `stage` parameter も `**kwargs` も持たない。

3. Failure row は `self.STAGE` で書く。

   repository は `projection.stage` を読まない。従来の
   `projection.stage or Stage.X` 経路は、guard を足すのではなく削除する。

4. `FailureProjection.stage` は削除する。

   `FailureProjection` は引き続き次の失敗属性を運ぶ:
   `failure_kind`, `retryability`, `failure_action`, `code`, `failure_reason`。

5. Error class の `STAGE` は削除する。

   `Error.STAGE` は、event stage を運ぶ役割と marker 判定に参加する役割の 2 つを
   兼ねていた。event-stage の責務は audit repository へ移す。marker 判定は既存の
   marker 属性で行う。

6. `AUDIT_FAILURE_MARKER` や marker mixin は追加しない。

   `project_marker_failure()` は、既存の duck-typed marker contract を満たす場合だけ
   その例外を audit marker とみなす。

   - `failure_kind` または `FAILURE_KIND` が存在し、空でない。
   - `RETRYABILITY` が `Retryability` enum value である。
   - `code` または `CODE` が存在し、空でない。
   - `FAILURE_ACTION` が存在する場合は `FailureAction` enum value である。

   現在の marker family である curation / assessment / embedding / briefing /
   acquisition は、すでに `RETRYABILITY` を持っている。新しい boolean marker は
   もう一つの冗長な契約を増やすだけなので追加しない。

7. Backfill は `backfill_stage` から `Stage.BACKFILL_*` を導出する。

   `BackfillAuditRepository` は design 上 multi-stage である。public method は
   `backfill_stage: Literal["curate", "assess", "embed"]` を受け取り、audit event
   stage を内部で導出する。

   ```python
   _STAGE_BY_BACKFILL_STAGE = {
       "curate": Stage.BACKFILL_CURATE,
       "assess": Stage.BACKFILL_ASSESS,
       "embed": Stage.BACKFILL_EMBED,
   }
   ```

   caller は `stage=Stage.BACKFILL_*` と `backfill_stage=...` を同時に渡さない。

8. Ready-build outcome prefix は repository stage SSoT を使う。

   audit repository 内の
   `project_ready_build_failure(stage_prefix="curation", ...)` のような呼び出しは、
   `stage_prefix=self.STAGE.value` に寄せる。これは DB stage 選択ではないが、
   手書き stage label の重複を減らす。

9. 観測 helper は別途型付けする。

   metric / span は stage constant を得るためだけに audit repository を import しない。
   audit persistence の外側にある helper では、直接 `Stage.X` を参照してよい。
   重要なのは、可能な範囲で raw string stage parameter をなくすこと。

   - `record_rate_limit_gate_skipped(stage: Stage, model: str)`
   - `record_injection_boundary_detected(stage: Stage)`
   - `article_stage` span helper は `stage=Stage.X.value` を設定する。

   `record_audit_dropped(stage: Stage)` はすでに `Stage` を受け取る。新しく型付けする
   metric helper も、attribute へは `stage.value` を入れる。

   injection boundary の structured log も対象に含める。metric だけを
   `Stage` 化して、隣接する `logger.warning(..., stage="curation")` /
   `stage="completion"` を raw string のまま残さない。

## 移行計画

### Step 1 - Repository constant と funnel

audit repository に `STAGE` / `BACKFILL_STAGE` constant を追加する。これらを注入する
private append funnel を追加する。repository business method は funnel を使うようにし、
直接の `stage=Stage.X` append call を削除する。

funnel は `**kwargs` ではなく、`PipelineEventRepository.append()` の `stage` 以外の
optional field を明示引数として持つ。

この step は単独でも価値があり、挙動を保つため低リスクである。

### Step 2 - Failure row は常に repository stage を使う

failure append path は同じ funnel を通して `self.STAGE` を使う。`projection.stage` は
読まない。

### Step 3 - `FailureProjection.stage` を削除する

`FailureProjection` から `stage` field を削除し、`project_marker_failure()` 内の
`FailureProjection(stage=stage, ...)` 構築 kwarg も削除する。projection 上の stage を
assert している test は更新する。代わりに、marker exception が期待する
`failure_kind`, `code`, `retryability` へ project されることを assert する。

分割変更にする場合、この step では `project_marker_failure()` の
`getattr(exc, "STAGE", None)` と `isinstance(stage, Stage)` gate を残してよい。
次 step で `Error.STAGE` と gate を同時に削除する。

### Step 4 - `Error.STAGE` を削除する

`project_marker_failure()` の gate 更新と同じ変更で、marker root class から `STAGE`
class variable を削除する。

`project_marker_failure()` に `isinstance(stage, Stage)` を残したまま `Error.STAGE` だけ
削除してはいけない。その場合、すべての marker exception が DB/unknown projection へ
silent に降格してしまう。

この変更では、各 `test_errors.py` にある `SomeError.STAGE == Stage.X` の assert も
更新対象とする。stage assert は repository constant の test か、marker classification
contract (`failure_kind`, `code`, `retryability`) の test へ移す。

### Step 5 - Backfill stage を導出する

`BackfillAuditRepository.append_item_event()` と `append_run_event()` から `stage`
parameter を削除する。event stage は `backfill_stage` から内部で導出する。

task-level wrapper である `_append_backfill_item_event()` /
`_append_backfill_run_event()` からも `stage` parameter を削除する。caller は
`backfill_stage` だけを渡し、wrapper 内の log / metric も導出された `Stage` を使う。

task-level の `pipeline_stage_span(Stage.BACKFILL_*, ...)` は、task entrypoint が span
label を所有しているため、直接 `Stage.X` を使い続けてよい。

### Step 6 - 観測 helper を型付けする

raw string stage helper は 2 種類に分けて扱う。

1. `stage` 引数を持つ helper は、closed stage label の raw string ではなく
   `Stage` を受け取る形へ変える。metric attribute へは `stage.value` を入れる。
2. `article_stage` のように `stage` 引数を持たず内部で span attribute を設定する helper は、
   literal string を `Stage.X.value` へ置き換える。

curation / completion の injection boundary は、metric helper と隣接 structured log の
両方から raw string stage を取り除く。必要であれば audit repository migration とは別
PR にする。

## 不変条件

- `Stage` enum は DB wire-value SSoT のままにする。
- Audit event stage selection は、その event を書く audit repository が所有する。
- Repository business method は任意の `stage` を受け取れない。
- Failure projection は event stage を運ばない。
- Error marker class は event stage を運ばない。
- Marker detection は stage ではなく失敗分類属性に基づく。
- Backfill caller は `backfill_stage` と食い違う event stage を渡せない。
- Raw string stage label は増やさない。新しい helper API は `Stage` を優先する。

## 非目標

- DB schema / migration の変更。
- `Stage` enum value の変更。
- `pipeline_events.payload.kind` の変更。
- すべての `Stage.X` 参照を audit repository 経由に強制すること。
- constant を得るためだけに observability helper から audit repository を import すること。
- `AUDIT_FAILURE_MARKER`、marker mixin、新しい marker base class の追加。
- 失敗分類 field (`failure_kind`, `retryability`, `failure_action`, `code`,
  `failure_reason`) の削除。

## 検証

構造的保証は test だけではなく API shape で作る。

主な保証:

- Repository public/business method は `stage` parameter を持たない。
- Repository append funnel は caller-provided な `stage` を持たず、`**kwargs` でも
  受けない。
- Failure append path は `projection.stage` を読まない。
- `Error.STAGE` 削除後も、`project_marker_failure()` は代表的な marker exception を
  正しく project する。
- stage を持っていた error test は、repository stage SSoT または marker classification
  contract の test へ置き換えられている。

有用な tripwire:

- `backend/app/audit/stages/*.py` 向けの focused static test:
  直接の `_events.append(stage=...)` は repository の private funnel 内だけ許可する。
- discovery-style projection test:
  curation / assessment / embedding / briefing / acquisition の代表 marker exception が、
  `STAGE` に依存せず non-unknown な `FailureProjection` へ project されること。
- backfill repository test:
  `backfill_stage="curate|assess|embed"` がそれぞれ
  `backfill_curate|backfill_assess|backfill_embed` を書くこと。
- raw string audit stage label の focused grep:
  injection boundary の metric/log と article-stage span helper に、新しい
  `stage="curation"` / `stage="completion"` などを残さないこと。

広範な `Stage.X` 禁止は避ける。admin read filter、test、span label、その他の
wire-value consumer は enum を直接参照してよい。
