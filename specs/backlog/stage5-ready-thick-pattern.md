# Stage 5 Embedding — 厚い Ready + 下流 Stage 自身が処理開始時に構築 (案 3)

Status: Stage 5 で 2026-05-12 完了。Stage 4 (Assessment) への横展開も 2026-05-12 完了
(`specs/backlog/stage4-ready-thick-pattern.md` 参照)。

## 問題提起

旧 Pattern A' (ID-only Ready) は以下 3 点で型として保証の実体が無いことが
2026-05-11 の再評価で判明した。

1. **`int` の nominal wrapper で中身に invariant が無い**: `ReadyForEmbedding(int)`
   のような ID-only Ready は `int` を包む薄い型であり、`InScope` のような
   「型を通った時点で内容が確定する」価値を持たない。

2. **kiq enqueue → 実行までに DB 状態が変わる**: 上流 Stage 4 task が
   `try_advance_from` を呼んで Ready を構築 → kiq enqueue → worker pickup →
   実際の処理開始までに DB 状態は変わりうる。Service は到着した Ready が
   表す precondition を真実とみなせず、DB fetch + None チェックで実質的に
   再検証している。

3. **「Service の precondition 分岐を消す」当初目的に反する**: Pattern A' は
   Service から precondition 分岐を消すことを目的としていたが、結局 Service
   入口で DB fetch + `RuntimeError` fail-fast による再検証が必要になり、
   precondition 検査が「`RuntimeError` fail-fast」に名前を変えただけで責務は
   消えていない。

加えて、責務の主語が間違っていた:

- 上流 Stage 4 task が「下流 Stage 5 のための Ready」を構築する設計は、
  **「下流で次に進むことを上流が保証する」** 構造である
- これは BC 境界原則 (`feedback_bc_boundary_guarantees_downstream.md`) と矛盾
  しないものの、「処理開始時に処理ができることを Stage 自身が保証する」という
  Ready 型の本来の意味とずれている
- User 指摘 (2026-05-11): 「処理を開始するときに、処理ができることを保証する」

## 確定方針 (案 3)

### 設計の核心

Ready 型は **処理に必要な内容をすべて値として運ぶ厚い型** であり、
**下流 Stage 自身が処理開始時に DB から内容を fetch して構築** する。

```
Stage 4 Task ─────┐
                  └─ kiq (EmbeddingTrigger: analysis_id のみ) ──┐
                                                               ▼
                                                       Stage 5 Task
                                                               │
                                                               │ (処理開始)
                                                               ▼
                                          ReadyForEmbedding.try_advance_from
                                                               │
                                              ┌────────────────┴────────────────┐
                                              │                                 │
                                          構築成功                            構築失敗
                                       (厚い Ready 生成)                       (None)
                                              │                                 │
                                              ▼                                 ▼
                                  rate limit acquire                    skip (業務正常)
                                              │
                                              ▼
                                    EmbeddingService.execute
                                  (ready.text_for_embedding を
                                   そのまま embedder に渡す)
```

### Ready 型の責務 (3 つ)

1. **処理に必要な値の全揃え保証**: 処理 (AI 呼び出し + 永続化) に必要な値を
   型として保持。Service は Ready の field を参照すれば必要な値が揃っている
2. **precondition の構造保証**: 同 ID の処理が未実行であること等の業務
   precondition を `try_advance_from` で検証
3. **構築タイミングの保証**: Ready が存在する = 処理開始時点で DB から値 fetch
   + precondition verify が完了している (= 時間ずれゼロ)

### 構築者は下流 Stage 自身

- 上流 Stage 4 Task: 自分の処理完了後、`kiq` で **ID だけ** を `EmbeddingTrigger`
  に詰めて下流 Task に渡す
- 下流 Stage 5 Task: kiq で受けた ID を `ReadyForEmbedding.try_advance_from(id, repo)`
  に通す。Ready の構築タイミングは処理直前 → 時間ずれゼロ、kiq message は ID のみ
  で軽量、値は最新の DB 状態を反映

### Repository が atomic 1-query で構築

- `EmbeddingRepository.try_load_for_embedding(analysis_id) -> ReadyForEmbedding | None`
- 1 query で「行存在 + 未 embedded + text 取得」を atomic に判定し、満たす
  場合のみ `ReadyForEmbedding` を直接構築して返す
- 旧 `is_embedded_for` (cheap exists) + `fetch_text_for_embedding` (text fetch)
  の 2 method を統合
- Domain 層 `ReadyForEmbedding.try_advance_from` は本 method への thin delegate

### Rate limit acquire の順序

`generate_embedding` task では **Ready 構築 → rate limit acquire → Service.execute**
の順を厳守する。Ready 構築 (DB fetch + precondition 検証) を先に行うことで、
stale trigger (既 embedded など) で AI quota / Redis rate limit を消費する事態を
回避する。

### maintenance backfill の責務縮退

`backfill_embeddings` (cron task) も「投入数を見る」役割に縮退し、precondition
検証 + Ready 構築は下流 Stage 5 task に委ねる。各 `analysis_id` を
`EmbeddingTrigger` に詰めて kiq に流すだけ。stale trigger は Stage 5 task の
`generate_embedding_skipped` ログで観測する。

## 却下した案 (記録)

### 旧 Pattern A' (ID-only Ready) — 却下

`ReadyForEmbedding(int)` のように ID のみ持つ Ready。詳細は冒頭「問題提起」参照。

### 旧案 1 (厚い Ready + 上流 Task が構築) — 却下

上流 Stage 4 task が値を fetch して厚い Ready を構築し、kiq で下流に渡す設計。

却下理由:
- taskiq enqueue 時点で値が snapshot 凍結 → 実行時に DB の最新値とずれうる
- Redis Stream message size 増大 (長文 `summary` 等を毎 enqueue で運ぶ)
- 「上流が下流のために」次に進めることを保証する構造 → 責務の主語が間違っている

### 案 2 (Ready 廃止) — 却下

Ready 型を作らず、Service が直接 ID を受け取って入口で `exists_for_*` + 値 fetch
+ None チェック。

却下理由:
- 「処理に進める」を表現する型は必要 (User 指摘: 「ready のようにその処理に
  進めるという型は必要」)
- precondition rule が各 Service に分散、Domain (Ready 型) に rule を集約できない

## Stage 5 横展開後の状態

| 要素 | 旧 (Pattern A') | 新 (案 3) |
|---|---|---|
| `ReadyForEmbedding` | `analysis_id: int` のみ | `analysis_id` + `text_for_embedding` |
| kiq message | `ReadyForEmbedding(analysis_id)` | `EmbeddingTrigger(analysis_id)` |
| Ready 構築タイミング | 上流 Stage 4 task | 下流 Stage 5 task (処理開始時) |
| Repository method | `is_embedded_for` + `fetch_text_for_embedding` | `try_load_for_embedding` (1-query atomic) |
| Service 入口の text fetch | 必要 | 不要 (Ready が運ぶ) |
| Service の RuntimeError fail-fast | 「行不在」で発火 | race 後の「勝者読み戻し失敗」のみ |
| backfill_embeddings | `try_advance_from` + kiq(ready) | kiq(trigger) のみ |

## Stage 4 への横展開 (2026-05-12 完了)

`ReadyForAssessment` も同じ弱点 (薄い 3 fields / 上流 Task 構築 / AuditRepository
2-hop 逆引き) を持っていたため、Stage 4 でも案 3 を適用した。詳細は
`specs/backlog/stage4-ready-thick-pattern.md`。

主な成果:
- `ReadyForAssessment` を 5 fields (`extraction_id` / `translated_title` /
  `summary` / `article_id` / `source_name`) に拡張
- `AssessmentRepository.try_load_for_assessment` (1-query atomic) を新設、
  旧 `exists_in_scope` / `exists_out_of_scope` 2 method を削除
- `AssessmentAuditRepository` の `_article_id_for` / `_resolve_source_name`
  2-hop 逆引きを撤去 (Ready から直接読む)
- `AssessmentTrigger(extraction_id)` を kiq message として導入
- `extract_content` (Stage 3 → 4 chain) を Trigger 経由に
- `backfill_assessments` を `extraction_ids_pending_assessment` + Trigger に縮退

## 関連 memory

- `project_typed_pipeline_preconditions.md` (2026-05-11 確定版) — 案 3 の根拠
- `feedback_bc_boundary_guarantees_downstream.md` — 派生原則 (ID で繋ぐ) は
  撤回済、base 原則 (BC 境界が下流の信頼性を保証する) は維持
- `feedback_taskiq_basemodel_required.md` — taskiq kiq 引数は BaseModel(frozen=True)
  必須 → EmbeddingTrigger 設計と整合
- `feedback_failure_visibility.md` — race 後の None 検出は依然 fail-fast を維持

## 次のステップ

1. spec `specs/typed-pipeline-preconditions.md` を案 3 で再ドラフト
2. Stage 3 以前への適用判断 (Stage 3 Extraction も同 pattern の余地あり)
