# Question Answering Flow 境界整理仕様

## 位置付け

質問回答機能の実装済み direct 経路と evidence 経路を、責務の粒度が名前と
package 構造から読み取れる形に整理するリファクタリング仕様。

現在はユースケース全体の進行役、LLM 呼び出しを包む経路処理、出力検証を含む
処理がすべて `*Service` または単一 module に置かれている。本変更では語彙を
次の 3 段階に統一する。

```text
QuestionAnsweringOrchestrator
  ├─ DirectAnswerFlow
  │    └─ DirectAnswerGenerator
  └─ EvidenceAnswerFlow
       └─ EvidenceAnswerDraftGenerator
```

- **Orchestrator**: 質問回答ユースケース全体の計画・分岐・結果組み立て。
- **Flow**: 1 つの回答経路内の生成・検証・retry・fallback・観測。
- **Generator**: provider API を呼び、未検証の LLM 出力を返す adapter 境界。

本 slice は内部構造と内部シンボル名の整理であり、回答の意味論、API response、
DB、認証、provider 設定を変更しない。

## Problem

1. `QuestionAnsweringService`、`DirectAnswerService`、
   `AnswerSynthesisService` が異なる粒度の責務を同じ `Service` と表現しており、
   同列のサービスが複数存在するように見える。
2. direct 経路は `direct.py`、evidence 経路は `synthesis.py` と命名が非対称で、
   どちらも回答経路であることを構造から読み取りにくい。
3. `direct.py` は contract と実装を 1 module に持ち、`synthesis.py` はさらに
   contract、flow、決定的補正、citation 検証、audit 呼び出しまで持つ。
4. `synthesis` が evidence 自体の品質評価なのか、回答生成なのか、LLM 出力の
   品質ゲートなのかが名前だけでは判別しにくい。
5. 上位 orchestration が依存する port と、composition が生成する具象実装の
   境界がファイル配置から分かりにくい。

## Evidence

- `backend/app/agent/answering/service.py`
  - `QuestionAnsweringService` が planning、direct/evidence dispatch、evidence
    collection、final result 組み立てを行う最上位ユースケース。
  - `DirectAnswerer` / `EvidenceAnswerSynthesizer` の Protocol に依存する。
- `backend/app/agent/answering/direct.py`
  - `DirectAnswerDraft`、generator / answerer Protocol、typed error、
    `DirectAnswerService`、retry・audit helper を同居させている。
- `backend/app/agent/answering/synthesis.py`
  - strict / raw draft、generator / synthesizer Protocol、typed error、
    `AnswerSynthesisService`、決定的補正、citation 検証、retry・fallback・
    audit helper を同居させている。
- `backend/app/agent/answering/evidence.py`
  - `EvidenceCollectionOutcome` を回答専用の `AnswerEvidenceItem` に変換する。
- `backend/app/agent/answering/ai/`
  - direct / evidence の adapter、prompt、spec が同じ package に並ぶ。
- `backend/app/agent/composition.py`
  - 最上位ユースケース、2 つの経路実装、2 つの generator を組み立てる。
- `backend/tests/agent/answering/`
  - orchestration、direct、synthesis、evidence normalization、provider adapter の
    現在の振る舞いを検証している。

## 責務境界

### Evidence quality との境界

`EvidenceAnswerFlow` は evidence 自体の品質管理サービスではない。
次のような evidence 固有の品質評価は本 slice の責務外とする。

- source の信頼性・権威性評価
- 質問との意味的関連性の再評価
- 情報の鮮度判定
- 複数 source による裏付け
- evidence 間の意味的矛盾検出
- evidence の ranking / semantic deduplication

`EvidenceAnswerFlow` が保証するのは、与えられた evidence を入力として
生成した回答 draft の構造と citation integrity である。

- answer / sufficiency / missing aspects の strict contract
- 本文 citation marker と `cited_refs` の整合
- citation ref が入力 evidence に存在すること
- 補完可能な LLM 出力欠陥の決定的補正
- 補完不能欠陥の retry / fallback

「すべての主張が evidence に意味的に含意されること」は prompt による
best-effort であり、この flow の決定的保証には含めない。

### Contract と Flow の境界

`contract.py` は「何を保証するか」を所有する。

- 入出力 Pydantic model
- 上位 orchestration が依存する capability Protocol
- provider adapter が実装する generator Protocol
- 境界を越えて分類・伝播される typed error

`flow.py` は「その保証をどう実現するか」を所有する。

- generator 呼び出し
- response の検証・補正の起動
- retry / fallback / typed error 伝播
- audit / metrics 記録
- provider metadata の取得

`validation.py` は evidence 回答固有の「未検証 draft を strict draft に確定する
決定的処理」を所有する。direct 回答の検証は citation を持たず小さいため、
専用 `validation.py` を作らず `direct_answer/flow.py` 内に残す。

## 合意済みの設計判断

### 1. Service 語彙を回答経路から除く

| 現在 | 変更後 | 粒度 |
|---|---|---|
| `QuestionAnsweringService` | `QuestionAnsweringOrchestrator` | ユースケース全体 |
| `DirectAnswerService` | `DirectAnswerFlow` | direct 回答経路 |
| `AnswerSynthesisService` | `EvidenceAnswerFlow` | evidence 回答経路 |
| `DirectAnswerGenerator` | 変更なし | direct provider 境界 |
| `EvidenceAnswerDraftGenerator` | 変更なし | evidence provider 境界 |

`Service` を別の曖昧な接尾辞に置き換えず、実際の責務を表す
`Orchestrator` / `Flow` / `Generator` を使う。

### 2. direct / evidence の package を対称にする

目標構造は次とする。

```text
backend/app/agent/answering/
├── __init__.py
├── orchestration.py
├── audit.py
├── metrics.py
├── direct_answer/
│   ├── __init__.py
│   ├── contract.py
│   ├── flow.py
│   └── ai/
│       ├── __init__.py
│       ├── gemini.py
│       ├── prompt.py
│       └── spec.py
└── evidence_answer/
    ├── __init__.py
    ├── contract.py
    ├── evidence.py
    ├── validation.py
    ├── flow.py
    └── ai/
        ├── __init__.py
        ├── gemini.py
        ├── prompt.py
        ├── schema_tool.py
        └── spec.py
```

- `answering/direct.py`、`answering/synthesis.py`、`answering/evidence.py`、
  `answering/service.py` は移行完了後に削除する。
- 旧 module を forwarding alias として残さない。二つの正本を作らず、repository
  内の参照を同じ変更で一括更新する。
- `audit.py` / `metrics.py` は既存の分類語彙・観測基盤を共有しているため
  package 直下に残す。この slice では分割しない。

### 3. Orchestrator は Protocol のみに依存する

`QuestionAnsweringOrchestrator` は次へ依存する。

- `QuestionPlanner`
- `EvidenceCollector`
- `DirectAnswerer`
- `EvidenceAnswerer`
- `AnswerProgressReporter`

具象 `DirectAnswerFlow` / `EvidenceAnswerFlow` や Gemini adapter を
import しない。具象型の組み立ては `composition.py` だけが担当する。

### 4. direct と evidence の port 語彙を揃える

| 現在 | 変更後 |
|---|---|
| `DirectAnswerer.answer()` | 変更なし |
| `EvidenceAnswerSynthesizer` | `EvidenceAnswerer` |
| `EvidenceAnswerSynthesizer.synthesize()` | `EvidenceAnswerer.answer()` |
| constructor `synthesizer=` | `evidence_answerer=` |
| field `_synthesizer` | `_evidence_answerer` |

両経路は上位から見ると「回答する capability」であり、内部実装が direct text
生成か evidence synthesis かは Flow 内へ閉じる。

### 5. evidence draft 名を経路固有にする

| 現在 | 変更後 |
|---|---|
| `AnswerDraft` | `EvidenceAnswerDraft` |
| `RawAnswerDraft` | `RawEvidenceAnswerDraft` |
| `AnswerDraftGenerationInvalidError` | `EvidenceAnswerDraftGenerationInvalidError` |
| `AnswerDraftInvalidError` | `EvidenceAnswerDraftInvalidError` |
| `AnswerSufficiency` | `EvidenceAnswerSufficiency` |

`DirectAnswerDraft` / `DirectAnswerInvalidError` はすでに経路が明示されているため
変更しない。

### 6. contract.py の内容

`direct_answer/contract.py` は次を所有する。

```text
DirectAnswerDraft
DirectAnswerGenerator (Protocol)
DirectAnswerer (Protocol)
DirectAnswerInvalidError
```

`evidence_answer/contract.py` は次を所有する。

```text
EvidenceAnswerSufficiency
EvidenceAnswerDraft
RawEvidenceAnswerDraft
EvidenceAnswerDraftGenerator (Protocol)
EvidenceAnswerer (Protocol)
EvidenceAnswerDraftGenerationInvalidError
EvidenceAnswerDraftInvalidError
```

- contract は audit / metrics、Gemini、retry 回数、fallback 文言を知らない。
- Pydantic validator と method の引数は現在の contract を維持する。
- typed error は provider adapter、validation、flow、上位 catch 面で共有する
  境界語彙であるため contract に置く。
- agent 全体の `AnswerQuestionInput` / `AnswerQuestionResult` などは既存の
  `app.agent.contract` に残す。

### 7. flow.py の内容

`direct_answer/flow.py` は現在の `DirectAnswerService` の実装責務を移す。

```text
DirectAnswerFlow
blank response の retry 制御
direct 回答から citation marker を除去する決定的処理
provider / blank failure の分類と typed error 伝播
direct audit / metrics 記録 helper
```

`evidence_answer/flow.py` は現在の `AnswerSynthesisService` のうち、
オーケストレーション責務を移す。

```text
EvidenceAnswerFlow
generator 呼び出し
response-boundary failure の retry 制御
fallback draft の生成
defect / attempt failure / final event の audit
outcome metrics
```

Flow の公開 method は両方 `answer(...)`、generator の公開 method は両方
`generate(...)` とする。

### 8. evidence validation.py の内容

`evidence_answer/validation.py` は現在の `synthesis.py` から次を移す。

```text
finalize_evidence_answer_draft(raw, *, evidence)
  -> tuple[EvidenceAnswerDraft, list[str]]

本文 marker から cited_refs を初出順で導出
cited_refs / missing_aspects の空・非文字列・重複除去
insufficient missing_aspects の決定的補完
sufficiency 値の検証
answered marker 必須検証
evidence に存在しない citation ref の検出
補正内容を示す defect code
```

- `finalize` は validate だけでなく、許可された決定的補正を行った後に strict
  draft を構築するためこの名前を使う。
- retry / fallback / audit は決めない。補完不能時は contract の typed error
  または Pydantic `ValidationError` を送出し、Flow が扱う。
- orchestration 側の citation backstop は独立した防御境界として残す。
  重複排除だけを目的に同じ helper を共有しない。

### 9. evidence.py は evidence 回答 package に置く

`AnswerEvidenceItem` と `normalize_answer_evidence()` は
`evidence_answer/evidence.py` へ移す。

これは検索・収集結果そのものの汎用 model ではなく、次を行う回答経路専用の
projection である。

- internal / external provenance を `AnswerSource` に変換する。
- answer marker の結合キーとなる `source_ref` を採番する。
- prompt に渡す text を summary / key points / claim / snippet から構築する。

したがって `evidence_collection` へは移さない。

### 10. AI adapter は各経路 package に置く

| 現在 | 変更後 |
|---|---|
| `answering/ai/gemini_direct.py` | `answering/direct_answer/ai/gemini.py` |
| `answering/ai/gemini_direct_prompt.py` | `answering/direct_answer/ai/prompt.py` |
| `answering/ai/gemini_direct_spec.py` | `answering/direct_answer/ai/spec.py` |
| `answering/ai/gemini.py` | `answering/evidence_answer/ai/gemini.py` |
| `answering/ai/gemini_prompt.py` | `answering/evidence_answer/ai/prompt.py` |
| `answering/ai/gemini_spec.py` | `answering/evidence_answer/ai/spec.py` |
| `answering/ai/schema_tool.py` | `answering/evidence_answer/ai/schema_tool.py` |

class 名は provider と capability が明確なため維持する。

- `GeminiDirectAnswerGenerator`
- `GeminiEvidenceAnswerDraftGenerator`
- provider response defect enum / error
- prompt renderer / spec class

ファイル移動によって prompt 本文、model、generation config、response schema、
rate limit policy、call signature の意味を変更しない。

### 11. 観測上の識別子を維持する

この slice は構造整理であり observability migration ではない。次は変更しない。

- metric 名と label / value
- audit event の serialized `kind`
- audit outcome code の serialized value
- failure kind / retry disposition
- prompt version / call signature の意味

Python class 名に残る `AnswerSynthesis*` audit 語彙は本 slice では rename
しない。dashboard / alert / log query への影響を伴う変更は別 slice とする。

## 依存方向

```text
QuestionAnsweringOrchestrator
  -> direct_answer/contract.py の DirectAnswerer
  -> evidence_answer/contract.py の EvidenceAnswerer

DirectAnswerFlow
  -> direct_answer/contract.py の DirectAnswerGenerator

EvidenceAnswerFlow
  -> evidence_answer/contract.py の EvidenceAnswerDraftGenerator
  -> evidence_answer/validation.py

direct_answer/ai/*
  -> direct_answer/contract.py の Generator contract

evidence_answer/ai/*
  -> evidence_answer/contract.py の Generator contract
  -> evidence_answer/evidence.py の prompt input model

composition.py
  -> Orchestrator + Flow + Generator の具象型を組み立てる
```

詳細規則:

- orchestration は Flow / Gemini の具象型を import しない。
- Flow は Gemini adapter を import せず、注入された Generator Protocol
  だけに依存する。
- AI adapter は orchestration を import しない。
- contract は flow / validation / audit / metrics / provider を import しない。
- validation は provider、retry、audit、metrics を import しない。
- direct と evidence_answer package は相互 import しない。
- 両経路で共有する agent-wide model は `app.agent.contract` を使う。
- composition 以外で具象 Flow を要求するコードを新たに作らない。

## 実行フロー

### Direct 経路

```text
QuestionAnsweringOrchestrator
  -> DirectAnswerer.answer()
  -> DirectAnswerFlow
  -> DirectAnswerGenerator.generate()
  -> blank / citation marker cleanup
  -> retry または DirectAnswerDraft
```

### Evidence 経路

```text
QuestionAnsweringOrchestrator
  -> EvidenceCollector.collect()
  -> normalize_answer_evidence()
  -> EvidenceAnswerer.answer()
  -> EvidenceAnswerFlow
  -> EvidenceAnswerDraftGenerator.generate()
  -> finalize_evidence_answer_draft()
  -> retry / fallback または EvidenceAnswerDraft
  -> Orchestrator が sources / final status / retrieval summary を組み立てる
```

## Invariants

### 振る舞い

- planner が選ぶ direct / internal / external / mixed の dispatch を変えない。
- direct 成功は `status="answered"`、sources / missing aspects は空。
- direct の blank 応答だけを 1 回 retry する。
- direct の分類済み失敗は audit / metric を記録後、既存 typed error を伝播する。
- evidence は raw draft を決定的補正後に strict draft へ変換する。
- evidence の補完不能な response-boundary failure は既存分類に従って retry
  または fallback する。
- answer 本文 citation marker が cited source の正本であり、存在しない ref を
  final result に通さない。
- evidence 0 件でも evidence flow を呼び、valid な insufficient draft
  または fallback を返す。
- final result の `status` は draft だけでなく retrieval の unmet requirement、
  missing aspects、sources を使って orchestration が再導出する。
- 想定外例外を新たに catch / fallback しない。

### Contract

- `AnswerQuestionInput` / `AnswerQuestionResult` の Pydantic shape を変えない。
- FastAPI schema / OpenAPI / frontend generated type を変えない。
- `RetrievalPlan` / `EvidenceCollectionOutcome` の shape を変えない。
- progress stage / event の値を変えない。
- citation marker `[[N]]` の形式を変えない。
- audit / metrics の外部識別子を変えない。

### 実装

- provider API key は settings 経由で扱い、`.env` を読まない・表示しない・
  編集しない。
- Gemini prompt / spec の内容を構造整理と同時に調整しない。
- compatibility alias や旧 module forwarding file を残さない。
- 循環 import を service locator や runtime import の追加で隠さず、依存方向を
  直して解消する。
- 重複排除だけを目的に direct / evidence 共通 base Flow を作らない。
- audit / metrics helper の共通化を追加しない。

## Non-goals

- evidence の信頼性・関連性・鮮度・矛盾を評価する品質管理機能。
- prompt 改善、model 変更、generation config の変更。
- retry 回数、fallback 文言、failure classification の変更。
- API endpoint / response shape / status code の変更。
- DB schema / SQLAlchemy model / Alembic migration。
- 認証・認可の変更。
- dependency の追加・更新。
- audit event / metric namespace の rename。
- direct / evidence 共通 Flow 基底 class の導入。
- streaming / conversation / run execution 境界の変更。

## 移行手順

1. `direct_answer/` と `evidence_answer/` package を作成する。
2. model / Protocol / typed error を新しい `contract.py` へ移し、validator と
   method signature の意味が不変であることを確認する。
3. evidence の決定的補正・citation 検証を `validation.py` へ移し、既存
   synthesis test を validation / flow test に責務別に分ける。
4. `DirectAnswerFlow` と `EvidenceAnswerFlow` を `flow.py` へ移す。
5. evidence port の `synthesize()` を `answer()` に rename し、orchestration
   と fake 実装を同時に更新する。
6. `AnswerEvidenceItem` と normalization を `evidence_answer/evidence.py` へ
   移す。
7. provider adapter / prompt / spec / schema tool を各経路の `ai/` へ移す。
8. `QuestionAnsweringService` を `QuestionAnsweringOrchestrator` へ rename し、
   `orchestration.py` へ移す。
9. `composition.py`、queue task、probe、package export、tests の import と
   constructor keyword を更新する。
10. repository 全体を `rg` し、旧 module path / class 名 / `synthesize()`
    呼び出しが残っていないことを確認する。
11. 旧 module と空になった `answering/ai/` package を削除する。
12. `/check` スキルに従って format、lint、type、test を実行する。

## Changed Files

想定する主な変更範囲。実装時に同じ責務の参照元が見つかった場合は import
追従だけを追加してよい。

```text
backend/app/agent/answering/__init__.py
backend/app/agent/answering/orchestration.py                         (new)
backend/app/agent/answering/direct_answer/__init__.py                (new)
backend/app/agent/answering/direct_answer/contract.py                (new)
backend/app/agent/answering/direct_answer/flow.py                (new)
backend/app/agent/answering/direct_answer/ai/*                       (new/move)
backend/app/agent/answering/evidence_answer/__init__.py              (new)
backend/app/agent/answering/evidence_answer/contract.py              (new)
backend/app/agent/answering/evidence_answer/evidence.py              (new/move)
backend/app/agent/answering/evidence_answer/validation.py            (new)
backend/app/agent/answering/evidence_answer/flow.py              (new)
backend/app/agent/answering/evidence_answer/ai/*                     (new/move)
backend/app/agent/answering/audit.py                                 (import update)
backend/app/agent/composition.py
backend/app/queue/tasks/agent_run.py                                 (typed error import)
backend/scripts/probe_question_answering.py

backend/app/agent/answering/service.py                               (delete)
backend/app/agent/answering/direct.py                                (delete)
backend/app/agent/answering/synthesis.py                             (delete)
backend/app/agent/answering/evidence.py                              (delete)
backend/app/agent/answering/ai/                                     (delete after move)

backend/tests/agent/answering/test_service.py                        (move/rename)
backend/tests/agent/answering/test_direct.py                         (move/split)
backend/tests/agent/answering/test_synthesis.py                      (move/split)
backend/tests/agent/answering/test_evidence.py                       (move)
backend/tests/agent/answering/ai/*                                  (move/update)
```

推奨する test 構造:

```text
backend/tests/agent/answering/
├── test_orchestration.py
├── direct_answer/
│   ├── test_flow.py
│   └── ai/
│       ├── test_gemini.py
│       └── test_prompt_schema.py
└── evidence_answer/
    ├── test_evidence.py
    ├── test_validation.py
    ├── test_flow.py
    └── ai/
        ├── test_gemini.py
        └── test_prompt_schema.py
```

## Tests

### Contract

1. `DirectAnswerDraft` の non-blank contract が維持される。
2. `EvidenceAnswerDraft` の answered / insufficient validator が維持される。
3. raw evidence draft が lenient な provider boundary のままである。

### Direct Flow

1. valid text が `DirectAnswerDraft` になる。
2. citation marker が direct answer から除去される。
3. blank 応答が previous error 付きで 1 回だけ retry される。
4. blank 全滅時は `DirectAnswerInvalidError` を記録後に伝播する。
5. provider error と想定外例外の既存伝播規則が維持される。
6. audit recorder failure が回答を止めない。
7. metric 名・label・値が既存と一致する。

### Evidence Validation

1. marker から `cited_refs` を初出順 unique で導出する。
2. blank / duplicate / non-string list item を既存規則どおり補正する。
3. insufficient の missing aspects を既存定型文で補完する。
4. answered marker なし、不明 sufficiency、不実在 marker を拒否する。
5. evidence 0 件で valid insufficient draft を構築できる。
6. defect code が既存文字列と一致する。

### Evidence Flow

1. valid raw draft を finalized draft として返す。
2. 補完可能欠陥を audit 後に成功として返す。
3. 補完不能欠陥を previous error 付きで 1 回 retry する。
4. retry 全滅時に既存 fallback draft を返す。
5. provider failure classification と想定外例外の扱いを維持する。
6. audit event / metric の serialized 値が既存と一致する。

### Orchestration

1. `NoRetrievalPlan` は direct answerer だけを呼ぶ。
2. retrieval plan は collector と evidence answerer を呼ぶ。
3. evidence 0 件でも evidence answerer を呼ぶ。
4. cited marker に対応する source だけを final result に含める。
5. unmet requirement が answered draft を insufficient result に cap する。
6. progress stage の順序と値を維持する。
7. typed error・想定外例外を新たに握りつぶさない。

### Import / Composition

1. `composition.py` が Orchestrator、Flow、Generator を組み立てる。
2. worker task の direct typed error catch が新しい import path で維持される。
3. probe の direct / evidence 両モードが新しい構造を import できる。
4. 旧 module path と旧 class 名の参照が repository に残らない。

## Verification

実装変更後は `/check` を使用し、format、lint、type check、回答関連 test、
queue catch 面の関連 test を実行する。

- 実 API probe は provider key と外部通信を必要とするため unit / integration
  suite と分ける。未実行の場合は理由を明記する。
- OpenAPI shape は変更しないため `/gen-types` は不要。Pydantic API schema に
  差分が生じた場合は本仕様からの逸脱として停止し、原因を確認する。

## Done

- `Orchestrator → Flow → Generator` の語彙が class 名と package 構造で
  一致している。
- direct / evidence の contract と flow が対称な位置にある。
- evidence の draft finalization / citation validation が Flow から分離され、
  provider・retry・audit に依存していない。
- Orchestrator は具象 Flow / Gemini adapter に依存していない。
- `AnswerEvidenceItem` が evidence 回答専用 projection として配置されている。
- 旧 module path、旧 `*Service` class、旧 evidence `synthesize()` 呼び出しが
  repository に残っていない。
- API、DB、認証、prompt、model、retry、fallback、audit / metrics の外部的な
  振る舞いに差分がない。
- `/check` の format、lint、type、関連 test が green。
