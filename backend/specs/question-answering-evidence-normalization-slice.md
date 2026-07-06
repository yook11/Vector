# Answer Evidence 正規化 slice 仕様

## 位置付け

Q&A エージェントの工程 3。retrieval 結果 (`RetrievalOutcome`) を、synthesizer が
出どころを意識せず消費できる統一 evidence に正規化する。各 item は
「synthesizer が読む本文 (text)」と「provenance の正本 (`AnswerSource`)」を
対で持ち、source_ref 採番と最終 `sources` の対応をこの工程で構造的に固定する。

工程と名前の対応 (確定済み語彙):

| 工程 | 名前 | 状態 |
|---|---|---|
| 最上位ユースケース (`answer(input)`) | `QuestionAnsweringService` | 将来 slice |
| 工程 1: プラン作成 | `QuestionPlanningService` | 実装済み |
| 工程 2: plan を読んで retrieval 起動 | `QuestionPlanRetrievalService` | 実装済み |
| 工程 3: evidence 正規化 (本 slice) | `AnswerEvidenceNormalizer` (概念名) | 本 slice |
| 工程 4: 回答文合成 | `AnswerSynthesizer` | 後続 slice |

工程 3 の実装形態は class ではなく純関数とする (状態・設定・I/O を持たないため)。
`AnswerEvidenceNormalizer` は仕様上の工程名で、実装名は
`normalize_answer_evidence()`。

## Problem

- internal (`InternalArticleSearchHit`) と external (`ExternalSearchEvidence`)
  は型が全く異なり、synthesizer に生のまま渡すと出どころ分岐が synthesizer に
  漏れる。本文フィールドまで含めて統一しないと、プロンプト組み立てが kind で
  分岐し、吸収が半分残る。
- source_ref 採番の正本と、最終 `AnswerQuestionResult.sources` の構築が別の
  場所に分かれると、引用番号と source の対応ズレを作りやすい
  (non-direct answered は source 必須: `contract.py` の provenance validator)。
- **id 空間の不一致**: 内部検索 hit は `curation_id` (分析 BC 内部の同一性)
  しか運んでおらず、公開 /news id 空間 (`AnalyzedArticleRecord.id`) を
  持ち出していない。このままでは internal evidence から UI の記事に到達
  できない。

## Evidence

- `backend/app/services/articles.py` — `ArticleBrief(id=analysis.id, ...)`。
  公開 /news id 空間 = `AnalyzedArticleRecord.id`。
- `backend/app/agent/internal_retrieval/article_search.py`
  - SELECT は `curation_id` のみで `AnalyzedArticleRecord.id` を取っていない。
  - `InternalArticleContent`: title / summary / key_points / mentions /
    published_at。
- `backend/app/agent/external_search/contract.py` — `ExternalSearchEvidence`:
  source_ref (external package ローカル) / task_index / title / url (SafeUrl) /
  source_name / published_at / claim / why_selected / snippet。
  `ResearchTaskReport.missing` は task 単位の不足情報。
- `backend/app/agent/answering/service.py` — `RetrievalOutcome`
  (internal_hits / external_search / unmet_requirements)。
- `backend/app/agent/contract.py` — `AnswerSource` (internal_article /
  external_url の discriminated union)。provenance に必要な情報
  (source_ref / title / url / article_id / published_at / source_name /
  snippet) を既に全て持つため、normalizer がこれを直接構築する。
  `InternalArticleSource.article_id` は対外契約名として維持
  (watchlist API の公開 id 名と整合)。
- briefing key_articles 移行 — 公開 /news id 空間の判別語彙として
  `assessment_id` が確立済み。

## 前提変更 (internal_retrieval 側)

正規化の不変条件 C4 (公開 id) を成立させるための最小変更:

1. `PgVectorArticleSearchRepository.search_by_embedding` の SELECT に
   `AnalyzedArticleRecord.id` を追加する。
2. `InternalArticleSearchHit` に `assessment_id: int` (gt=0、required) を
   追加する。公開 /news id 空間であることを 1 文コメントで明示する。
3. ドメインオブジェクト `InScopeAnalyzedArticle` には触らない (分析 BC の
   同一性は curation_id のまま。公開参照は境界型 hit が運ぶ)。

DB schema 変更なし (query と境界型のみ)。migration 不要。

## New Types / Function

`backend/app/agent/answering/evidence.py` (新規):

```python
class AnswerEvidenceItem(BaseModel):
    """synthesizer 向け本文と provenance 正本を対で持つ根拠 1 件。"""

    model_config = ConfigDict(frozen=True)

    source: AnswerSource   # source_ref 採番と provenance の正本
    text: str = Field(min_length=1)   # 出どころ非依存の本文


def normalize_answer_evidence(
    outcome: RetrievalOutcome,
) -> list[AnswerEvidenceItem]:
    ...
```

- 中間型なので `contract.py` には置かない (final result 用に保つ)。
  `AnswerSource` は contract の安定型として参照する。
- 後続の assembly は「引用された item の `.source` を集めるだけ」で
  `AnswerQuestionResult.sources` を作れる (再構築・再採番をしない)。

### field 写像

internal (`InternalArticleSearchHit` →):

| 出力 | 入力 |
|---|---|
| `source` = `InternalArticleSource` | |
| `.source_ref` | normalizer 採番 |
| `.article_id` | `hit.assessment_id` (公開 /news id 空間。名前の対応はコメントで明示) |
| `.title` | `hit.content.title` |
| `.snippet` | `hit.content.summary` |
| `.published_at` | `hit.content.published_at` |
| `.source_name` | `None` (hit が news source 名を運んでいないため。将来 SELECT 拡張) |
| `text` | summary + key_points の決定的連結 (下記) |

external (`ExternalSearchEvidence` →):

| 出力 | 入力 |
|---|---|
| `source` = `ExternalUrlSource` | |
| `.source_ref` | normalizer 採番 (入力の `source_ref` は使わない) |
| `.url` | `evidence.url` 素通し |
| `.title` | `evidence.title` |
| `.snippet` | `evidence.claim` (UI のソースカードで意味が通る値を優先) |
| `.published_at` | `evidence.published_at` |
| `.source_name` | `evidence.source_name` |
| `text` | claim + snippet の決定的連結 (下記) |

### text 合成規則 (決定的)

```text
internal: summary
          key_points があれば改行し "- {point}" を入力順に連結
external: claim
          snippet があれば改行して連結
```

`why_selected` は selector の自己説明でありグラウンディング材料としては
ノイズ側のため text に含めない (Non-goals)。

## Invariants

- **A. 全件写像 (totality)**: 出力件数 =
  `len(internal_hits) + len(external_search.evidence)` (external_search が
  None なら external 分は 0)。silent drop も重複生成もしない。チャネル横断
  dedup はしない (internal は curation_id、external は URL で各 package が
  実施済み)。
- **B. source_ref 採番**:
  - `"1"`〜`"N"` の連番 (str、括弧なし)・一意・欠番なし。採番は
    `item.source.source_ref` に載る (これが正本)。
  - 順序: internal の入力順 → external の入力順の連結。各チャネル内の
    関連度順を壊さない。
  - 決定的: 同一入力 → 同一出力。乱数・時刻・uuid を使わない。
  - `ExternalSearchEvidence.source_ref` は external package ローカルの参照で
    あり、値に関わらず使わない。
- **C. provenance の保全**:
  - C1: external 由来 → `ExternalUrlSource`。url (SafeUrl) 必須・素通し。
  - C2: internal 由来 → `InternalArticleSource`。`article_id` 必須。
  - C3: field 写像表のとおり素通し (書き換えない)。text は上記の決定的
    合成規則のみ。
  - C4: `article_id` は公開 /news id 空間 (`hit.assessment_id` 由来)。
- **D. 純粋性と「不足系」の分離**: I/O なし・LLM なし。不足系の情報
  (`unmet_requirements`、`task_reports.missing`) は読まない・出力に含めない。
  **不足系は assembly (status / missing_aspects 判定を持つ orchestrator 側) が
  `RetrievalOutcome` から直接読む**。根拠系は normalizer から、不足系は
  outcome から、と読み口を対称に保つ。入力 `RetrievalOutcome` を変更しない
  (frozen で構造保証)。
- **E. 空入力は正常系**: evidence ゼロ → 空リストを返す (例外にしない)。
- **F. `RetrievalOutcome` の整合 validator (同居)**: `external_search` が
  set なのに `unmet_requirements` に `"external_search"` を含む状態は
  model_validator で構築不可にする。

## Non-goals

- `AnswerSynthesizer` / citation 照合 / `answer()` / `AnswerQuestionResult`。
- 不足系 (`unmet_requirements` / `task_reports.missing`) の evidence への
  取り込み (assembly が outcome から読む)。
- チャネル横断 dedup、公開日時等での再ランク (入力順の連結のみ)。
- `why_selected` / mentions / investor_take の text への取り込み (synthesis
  slice で必要になったら拡張する)。
- internal の `source_name` 充足 (SELECT 拡張は必要になったら)。
- `contract.py` の変更 (`InternalArticleSource.article_id` の rename 含む)。
- API endpoint / FastAPI DI / frontend 型生成。
- probe script の拡張。

## Changed Files

```text
backend/app/agent/answering/evidence.py            (新規)
backend/app/agent/answering/service.py             (RetrievalOutcome validator 追加)
backend/app/agent/answering/__init__.py            (export 追加)
backend/app/agent/internal_retrieval/article_search.py  (SELECT + hit field)
backend/tests/agent/answering/test_evidence.py     (新規)
backend/tests/agent/answering/test_service.py      (validator テスト + hit fixture 追従)
backend/tests/agent/internal_retrieval/            (assessment_id 追従 + 一致検証)
```

`InternalArticleSearchHit.assessment_id` は required とするため、既存テストの
hit 構築漏れは実行時に必ず検出される。

## Tests

期待値は fixture の入力から導出する。text の期待値は本仕様の合成規則から
組み立てる (実装関数を呼んで作らない)。1 不変条件 = 1 正本テスト。

`backend/tests/agent/answering/test_evidence.py`:

1. internal 2 + external 3 → 5 件、source の型内訳 2/3、source_ref が
   `"1"`〜`"5"` の連番一意 (A + B)。
2. 順序: internal[0]→"1"、internal[1]→"2"、external[0]→"3"… を zip 突合で
   検証 (B)。
3. external 全件の provenance 素通し: url / title / published_at /
   source_name が入力と一致、snippet == claim (C1, C3)。
4. internal 全件の provenance: article_id == hit.assessment_id、title、
   snippet == summary、published_at が入力と一致、source_name is None
   (C2, C4)。
5. text 合成規則: internal は summary + key_points、external は
   claim + snippet が仕様の規則どおり連結される。key_points 空 / snippet
   None の分岐も含む (C3)。
6. 入力 external の `source_ref` に `"external-9-9"` 等の紛らわしい値を与え、
   出力が位置ベースの連番になる (B: 入力 ref を使わない)。
7. 同一入力を 2 回 normalize → 完全同一出力 (B 決定性。uuid 採番への変異を
   殺す)。
8. 境界 4 種: 両方空 → [] / internal のみ / external_search=None /
   external_search はあるが evidence 空 → internal のみ採番 (E)。

`backend/tests/agent/answering/test_service.py` (追加):

9. `external_search` set + `unmet_requirements=["external_search"]` →
   ValidationError。`external_search=None` + 同 unmet → 構築可 (F)。

`backend/tests/agent/internal_retrieval/` (追従):

10. `search_by_embedding` が返す hit の `assessment_id` が検索対象行の
    `AnalyzedArticleRecord.id` と一致する (C4 の供給側検証)。

やらないテスト: external の URL dedup / internal の distance 順 (各 package の
所有テストが正本)、citation 照合 (synthesis slice)。

## Done

- `normalize_answer_evidence(outcome)` が Invariants A〜E を満たし、
  `AnswerEvidenceItem(source, text)` のリストを返す。
- `RetrievalOutcome` の整合 validator (F) が入っている。
- 検索 hit が `assessment_id` (公開 /news id 空間) を運んでいる。
- 上記テストが green。既存 suite (unit + integration) に regression なし。
- `contract.py` / API endpoint / DB schema には変更を加えない。
