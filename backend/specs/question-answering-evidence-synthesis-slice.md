# Evidence 回答合成 (工程 4E) 実装 slice 仕様 (Slice B-1)

## 位置付け

Slice A rev.2 で port 分離した回答工程のうち、evidence 経路
(`EvidenceAnswerSynthesizer`) の実装を作る。実 Gemini adapter・二層構造
(補完 + retry + fallback)・audit / metrics・probe 貫通までが本 slice。
direct 経路 (`DirectAnswerer`) の実装は Slice B-2 の責務とする。

| 工程 | 名前 | 状態 |
|---|---|---|
| ユースケース `answer(input)` | `QuestionAnsweringService` | 実装済み (rev.2 dispatch) |
| 工程 1〜3 | planning / retrieval / evidence | 実装済み |
| 工程 4E: evidence 回答 | `EvidenceAnswerSynthesizer` port | **本 slice で実装** |
| 工程 4D: direct 回答 | `DirectAnswerer` port | Slice B-2 |

## Problem

- `EvidenceAnswerSynthesizer` port の向こう側が存在せず、検索した根拠から
  回答を生成できない。
- rev.2 の決定的前段 (根拠ゼロ → LLM を呼ばず定型文) はユーザーに何も
  役立つ情報を返さない。「根拠がないことを断ったうえで参考回答を返す」
  ほうが良い、という判断により廃止する (後述の設計判断 2)。

## Evidence

- `backend/app/agent/answering/synthesis.py` — `EvidenceAnswerSynthesizer`
  Protocol と strict な `AnswerDraft` (answered → cited_refs 非空・missing
  空 / insufficient → missing 非空 / non-blank 制約)。
- `backend/app/agent/answering/service.py` — 決定的前段 (evidence 空 →
  synthesizer スキップ) と citation 照合 (`AnswerDraftInvalidError`)。
  use case 側の照合は本 slice 後も backstop として残す。
- `backend/app/agent/planning/service.py` + `planning/ai/` — 鏡写しの正本:
  - `QuestionPlanningService`: draft generator port を包み、失敗分類 →
    in-request retry 1 回 (previous_error 付き) → safe fallback → audit /
    metrics。**工程の外には完成契約しか出さない**。
  - `plan_from_draft`: LLM 生出力 (lenient) → 決定的補完 → strict 契約の
    二層構造の前例。
  - `planning/ai/gemini_spec.py`: model 定数 + gen_config + response schema
    + rate limit policy + `compute_call_signature` による版管理の流儀。
    planner は `gemini-2.5-flash-lite`。
- `backend/app/agent/planning/audit.py` / `planning/metrics.py` —
  attempt failure / final event / outcome counter の語彙の前例。
- `backend/scripts/probe_question_answering.py` — 貫通 probe の器。
  external plan 固定 + `_UnreachableInternalSearch` stub の流儀。

## 合意済みの設計判断

1. **Provider / model**: Gemini `gemini-3.1-flash-lite` (Gemini API docs で
   Stable、ユーザー確認済み)。spec 定数 + rate limit policy +
   call signature は planner の gemini_spec 流儀を踏襲する。
2. **根拠ゼロでも synthesizer を呼ぶ (決定的前段の廃止)**。evidence 経路は
   evidence 空でも LLM を呼び、プロンプトで「引用できる根拠が無い場合は
   その旨を明確に断ったうえで、一般知識に基づく参考回答を述べ、断定を
   避ける」よう指示する。根拠ゼロのとき answered が **valid な synthesized
   draft / final result になることは不可能** (工程内の citation 照合が
   evidence 空で必ず落とす)。`AnswerDraft` 単体は cited_refs があれば構築
   できるため、この保証の所在は型ではなく工程内照合であり、該当ケース
   (evidence 空 + answered + cited_refs あり) は補完不能欠陥として retry →
   fallback される。この経路の最終出力は必ず insufficient (断り + 参考回答
   + missing)。コスト影響: 0 ヒット質問ごとに LLM 呼び出しが 1 回増える
   (許容済み)。
3. **status は配信ゲートではなく観測値**。status は「回答を返すか」の判定に
   使わない (回答文は answered / insufficient どちらでも必ずユーザーに
   届く)。status・defect・fallback 発生は metrics / audit の次元として記録し、
   planner / retrieval / prompt の改善ループに使う。insufficient の由来は
   LLM 自己申告と unmet cap (決定的) の 2 系統があり、どちらも観測値として
   同じ扱いとする。consumer (API / UI) は status を表示ラベルに使ってよいが、
   回答の抑制・差し替えに使わない。
4. **二層構造 (planner 鏡写し)**。LLM 生出力は lenient に受け、決定的に
   補完してから strict `AnswerDraft` に落とす。**use case は valid な
   draft しか見ない**。
   - 補完可能な欠陥 → 決定的補完 + defect 記録 (処理は止めない):
     - insufficient で missing_aspects が空 (blank 除去後含む) → 定型
       missing 1 件を補完。
     - missing_aspects / cited_refs 内の blank 要素・重複 → 除去。
   - 補完不能な欠陥 → previous_error 付き in-request retry 1 回 → fallback:
     - response が JSON / schema として不正。
     - answer が blank (回答本文は捏造できない)。
     - answered なのに cited_refs 空。
     - evidence に実在しない ref の引用。
     - answered なのに missing_aspects 非空。
   - fallback draft: insufficient / 定型 answer (「回答を生成できません
     でした」系) / 定型 missing。fallback でも result は必ず返る
     (処理を止めない)。
5. **citation 照合は工程内が正、use case は backstop**。synthesis 工程は
   evidence を受け取るので ref 実在性まで工程内で検証し、valid な draft
   だけを返す。use case 側の `AnswerDraftInvalidError` 照合は防御的
   backstop として残す (実 LLM 経路では到達しない)。
6. **audit / metrics は planner 鏡写し**。attempt failure / final event
   (synthesized / fallback) / defect 補完の記録。retry_used・status を
   次元に持つ outcome counter。文言・語彙の詳細は実装時に audit 基盤の
   流儀 (`planning/audit.py`) に合わせる。
7. **direct 経路の LLM 全滅は typed error 伝播** (B-2 で実装)。evidence
   経路と違い偽の answered を作らない。本 slice では扱わない。

## New Types / Structure

```text
backend/app/agent/answering/synthesis.py (追加)
  EvidenceAnswerDraftGenerator (Protocol)   # LLM adapter boundary
    async def generate(
        *, question, evidence, as_of, target_time_window,
        previous_error: str | None = None,
    ) -> RawAnswerDraft                      # lenient (str fields 未検証)

  AnswerSynthesisService                     # EvidenceAnswerSynthesizer 実装
    __init__(*, generator, audit_recorder=None)
    async def synthesize(...) -> AnswerDraft # 補完 / retry / fallback 込み

backend/app/agent/answering/ai/
  __init__.py
  gemini.py          # GeminiEvidenceAnswerDraftGenerator (実 API)
  gemini_prompt.py   # 経路専用プロンプト (evidence 接地 + 引用 + 断り指示)
  gemini_spec.py     # model="gemini-3.1-flash-lite" + gen_config
                     #   + response schema + rate limit + call signature
backend/app/agent/answering/audit.py       # attempt failure / final event
backend/app/agent/answering/metrics.py     # outcome counter
```

- `RawAnswerDraft` は lenient な中間形 (LLM 応答の形をそのまま受ける)。
  strict 化は `AnswerSynthesisService` の補完後に行う。
- 定型文言 (fallback answer / 補完 missing) は synthesis 工程の実装定数。
  rev.2 で service.py に残っている決定的前段用の定数はここへ移設する。

## 前提変更 (service.py)

- 決定的前段 (evidence 空 → synthesizer スキップ) を削除し、evidence 経路は
  常に synthesizer を呼ぶ。
- 対応するテスト (「synthesizer が呼ばれない」) は「evidence 空でも
  synthesizer が呼ばれ、insufficient draft がそのまま result になる」に
  置き換える。

## Invariants

- use case (`QuestionAnsweringService`) は valid な `AnswerDraft` しか
  受け取らない。補完・retry・fallback は synthesis 工程内に閉じる。
- retry / fallback の対象は**明示列挙した失敗のみ** (planner の
  `_PLANNER_AUDITED_ERRORS` 鏡写し): `AIProviderError` (失敗分類に従う) /
  response の JSON・schema 不正 / strict 契約違反 (補完不能欠陥、Pydantic
  ValidationError 含む)。この範囲内では必ず draft を返す (fallback)。
  **想定外例外 (バグ・キャンセル等) は握りつぶさず伝播する**。
  `except Exception` による包括 fallback を書かない。
- 補完 (defect) と fallback は必ず記録される (沈黙させない)。
- 根拠ゼロの evidence 経路で answered の result は構築不可能 (既存
  validator + citation 照合が保証。本 slice で新たな仕組みは足さない)。
- status / defect / fallback は観測値であり、回答文の配信を左右しない。
- プロンプトは evidence 経路専用。direct 用の指示を混ぜない。
- 秘密情報は settings 経由。プロンプトに evidence 以外の内部情報を
  入れない。
- `contract.py` / plan variant / port signature (`EvidenceAnswerSynthesizer`)
  は変更しない。

## Non-goals

- `DirectAnswerer` の実装 (Slice B-2)。
- API endpoint / FastAPI DI / frontend 型生成。
- プロンプトの品質チューニング・eval 整備 (動く正直な v1 まで)。
- task_reports.missing の synthesizer への供給 (必要になったら port 拡張)。
- rate limit 値の最適化 (planner と同等の保守的設定で開始)。
- progress event / timeline。

## Changed Files

```text
backend/app/agent/answering/synthesis.py        (service + generator port + raw draft)
backend/app/agent/answering/ai/                 (新規 package: gemini adapter 一式)
backend/app/agent/answering/audit.py            (新規)
backend/app/agent/answering/metrics.py          (新規)
backend/app/agent/answering/service.py          (決定的前段の削除)
backend/app/agent/answering/__init__.py         (export)
backend/scripts/probe_question_answering.py     (synthesize 貫通へ延長)
backend/tests/agent/answering/test_synthesis.py (新規: 二層構造)
backend/tests/agent/answering/test_service.py   (前提変更の追従)
```

## Tests

fake generator で `AnswerSynthesisService` の二層構造を検証する。

1. valid な生出力 → そのまま strict `AnswerDraft` になる (補完なし・
   defect 記録なし)。
2. 補完可能: insufficient + missing 空 → 定型 missing が補完され、defect が
   記録され、処理が止まらない。
3. 補完可能: missing / cited_refs の blank 要素・重複が除去される。
4. 補完不能 (answered + cited_refs 空) → previous_error 付きで 2 回目が
   呼ばれ、2 回目が valid なら採用される。
5. 2 回目も不正 → fallback draft (insufficient + 定型) が返り、fallback が
   記録される。
6. 不実在 ref の引用が工程内で検出され retry 対象になる (use case まで
   届かない)。
7. **evidence 空 + answered + cited_refs あり** → 補完不能欠陥として
   retry され、2 回目も同様なら fallback (insufficient) になる (P2 の
   保証所在 = 工程内照合、の正本テスト)。
8. AIProvider 例外の分類: retry 不能な失敗は即 fallback。
9. **想定外例外 (分類外の Exception) は fallback にならず伝播する**。
10. metrics: synthesized / fallback / defect の outcome が次元付きで
    記録される (capfire)。
11. service.py 前提変更: evidence 空でも synthesizer が呼ばれる (既存
    テストの置き換え)。

adapter (Gemini) はプロンプト render と応答 parse の unit を planner の
gemini テストの流儀で書く。実 API は probe のみ。

## Probe (手動貫通)

`probe_question_answering.py` を延長し、external plan 固定で
`QuestionAnsweringService.answer()` を実行する:

- planner: 固定 plan を返す script 内 stub (planner 実配線の貫通は
  B-2 以降の統合で行う。質問により internal が選ばれると host から DB に
  届かないため)。
- direct_answerer: 呼ばれたら raise する stub (external plan では不到達)。
- synthesizer: 実 Gemini (`gemini-3.1-flash-lite`)。
- 出力: answer / status / sources (source_ref, url, title) /
  missing_aspects / defect・fallback の有無。

成功判定は形で行う: validate 済み `AnswerQuestionResult` が返り、引用が
sources と整合していること。回答の内容には依存しない。

## Done

- `AnswerSynthesisService` + Gemini adapter が存在し、fake generator の
  unit テストが green。
- 決定的前段が廃止され、evidence 空でも「断り + 参考回答」の insufficient
  result が返る。
- defect / fallback / outcome が観測できる (audit / metrics)。
- probe で実 Gemini + 実 Tavily/DeepSeek を通した「検索 → 引用付き回答」の
  貫通が確認できる (実行はユーザー環境)。
- 既存 suite に regression なし。
