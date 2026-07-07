# Direct 回答 (工程 4D) 実装 slice 仕様 (Slice B-2)

## 位置付け

Slice A rev.2 で port 分離した回答工程のうち、direct 経路 (`DirectAnswerer`)
の実装を作る。実 Gemini adapter・retry・typed error 伝播・audit / metrics・
probe 貫通までが本 slice。

direct は「根拠なし回答」ではなく**「検索不要回答」**である。planner が
`NoRetrievalPlan` を選んだ質問 (挨拶・一般知識・アプリの使い方等) に対して、
検索せず自然に回答する工程。根拠がないことを断る必要はない。

| 工程 | 名前 | 状態 |
|---|---|---|
| ユースケース `answer(input)` | `QuestionAnsweringService` | 実装済み (rev.2 dispatch) |
| 工程 1〜3 | planning / retrieval / evidence | 実装済み |
| 工程 4E: evidence 回答 | `AnswerSynthesisService` + Gemini adapter | 実装済み (B-1) |
| 工程 4D: direct 回答 | `DirectAnswerer` port | **本 slice で実装** |

## Problem

- `DirectAnswerer` port の向こう側が存在せず、`NoRetrievalPlan` の質問に
  回答できない (probe では unreachable stub)。
- direct 経路には evidence 経路の fallback draft に相当する「安全な既定回答」
  が存在しない。失敗を隠す形 (嘘の answered / insufficient への偽装) を
  作らずに失敗を扱う設計が必要。

## Evidence

- `backend/app/agent/answering/direct.py` — `DirectAnswerDraft`
  (answer: NonBlankText のみ) と `DirectAnswerer` Protocol。
- `backend/app/agent/answering/service.py` — `NoRetrievalPlan` → direct
  dispatch は実装済み。成功時 `status="answered"` / sources 空 / missing 空。
- `backend/app/agent/answering/synthesis.py` + `answering/ai/` — 鏡写しの正本
  (B-1): generator port を包む工程 service、`_SYNTHESIS_AUDITED_ERRORS` の
  明示列挙、previous_error 付き in-request retry 1 回、audit / metrics。
- `backend/app/agent/answering/audit.py` —
  `classify_answer_synthesis_failure`: AIProviderError →
  `DO_NOT_RETRY_IN_REQUEST` / 応答不正 → `RETRY_IN_REQUEST` の分類語彙。
- `backend/app/agent/answering/ai/gemini_spec.py` — model
  `gemini-3.1-flash-lite` + gen_config + rate limit policy +
  `compute_call_signature` の流儀。
- `backend/scripts/probe_question_answering.py` — 貫通 probe の器
  (`_UnreachableDirectAnswerer` を実装に置き換える)。

## 合意済みの設計判断

1. **direct は検索不要回答**。成功したら answered (direct 経路に
   insufficient は存在しない)。回答は自然に行い、「根拠がない」ことを
   断らせない。
2. **出力は plain text (JSON にしない)**。draft が answer 1 フィールドのみで
   引用も sufficiency もないため、structured output にする理由がない。
   これにより「応答形式の不備」は **blank (空・空白のみ) の 1 種に潰れ**、
   JSON 不正 / schema 違反 / 引用不実在の失敗クラスが構造的に消える。
   lenient 中間型 (`RawAnswerDraft` 相当) も置かない (補完可能欠陥が
   存在しないため二層構造が不要)。
3. **失敗は typed error 伝播 (fallback draft を作らない)**。direct の失敗は
   「根拠不足」ではなく「回答生成そのものの失敗」であり、insufficient への
   変換は失敗の偽装になる。`QuestionAnsweringService.answer()` は catch せず
   素通しし、API 層 (次スライス) がエラー応答に落とす。
4. **retry は blank 応答のみ 1 回**。
   - blank → previous_error 付き in-request retry 1 回 → 再 blank なら
     `DirectAnswerInvalidError` (direct.py 所有の typed error) を raise。
   - `AIProviderError` → B-1 分類 (`DO_NOT_RETRY_IN_REQUEST`) に従い
     retry せず即伝播。**包まず素通し** (既に分類済みの typed error であり、
     direct 用に包み直しても情報が増えない)。
   - 想定外例外 → 分類・記録せずそのまま伝播 (`except Exception` を
     書かない)。
5. **エラー分類は「工程 × failure family」の 2 軸**。分類属性
   (failure_kind / `RequestRetryDisposition` / 将来の HTTP マッピング) は
   failure family で決まり、経路名を混ぜない。工程の識別は direct 専用の
   audit kind / metric counter が担う (同じ情報を分類属性に二重に
   持たせない)。一方、**例外型は工程所有であり、型名が産地を表すのは
   分類とは別の話**: `DirectAnswerInvalidError` は「direct 工程の blank
   契約違反」を表す型であって、分類属性に経路を焼くものではない。
   failure_kind / `RequestRetryDisposition` の語彙は B-1 と共有するが、
   型の統合・共通基底は作らない (evidence 側は fallback で吸収し伝播しない
   ため、産地の違う型を束ねると存在しない共通契約を示唆する)。
6. **分類済み失敗で raise する経路でも観測は必ず焼く**。attempt failure /
   final event (answered / failed) / metric を記録してから raise する。
   「direct 失敗の増加」「misrouting っぽい質問の流入」を改善ループで
   観測するため。想定外例外は記録の対象外 (判断 4 のとおり捕まえない)。
   misrouting はプロンプトで防御しない (audit / eval で観測する合意)。
7. **伝播面は direct 工程側の docstring に明記**。`DirectAnswerer` port /
   `DirectAnswerService` の docstring に「失敗時は
   `AIProviderError | DirectAnswerInvalidError` が伝播する」と記す。
   `contract.py` には書かない: `QuestionAnsweringAgent.answer()` は全経路を
   含む global contract であり、direct の 2 型だけを書くと「answer 全体の
   例外 surface はこれだけ」と誤読される (想定外例外は全経路から伝播しうる)。
   answer() 全体の例外 surface の文書化は、catch 面を設計する API 層
   スライスで行う。
8. **Provider / model は B-1 と同じ Gemini `gemini-3.1-flash-lite`**。
   spec 定数 + rate limit policy + call signature は gemini_spec 流儀を
   踏襲する (structured_output / response_schema なし)。
9. **Gemini adapter の共通処理は複製で書く**。client 初期化・blocked
   finish_reason 判定・`translate_gemini_error` は B-1 adapter と重複するが、
   重複排除だけを目的に抽象化しない。共通化は実装後の提案に留める。

## New Types / Structure

```text
backend/app/agent/answering/direct.py (追加)
  DirectAnswerInvalidError                   # blank 全滅を表す typed error
  DirectAnswerGenerator (Protocol)           # LLM adapter boundary
    async def generate(
        *, question: str, as_of: datetime,
        previous_error: str | None = None,
    ) -> str                                 # plain text (未検証)

  DirectAnswerService                        # DirectAnswerer 実装
    __init__(*, generator, audit_recorder=None)
    async def answer(*, question, as_of) -> DirectAnswerDraft
                                             # blank retry / typed error / 観測込み

backend/app/agent/answering/ai/
  gemini_direct.py         # GeminiDirectAnswerGenerator (実 API, plain text)
  gemini_direct_prompt.py  # direct 専用プロンプト (自然回答 + as_of + 日本語)
  gemini_direct_spec.py    # model="gemini-3.1-flash-lite" + gen_config
                           #   + rate limit + call signature (schema なし)
backend/app/agent/answering/audit.py (追加)
  DirectAnswerAttemptFailureEvent / DirectAnswerFinalEvent
  # kind="agent_direct_answer"、outcome: answered / failed
  # failure_kind / RequestRetryDisposition の語彙は synthesis と共有
backend/app/agent/answering/metrics.py (追加)
  # vector.agent.direct_answer.outcome counter
  # 次元: result ("answered" | "failed"), retry_used
```

- プロンプトは direct 専用。evidence 接地・引用・断りの指示を混ぜない。
  as_of を渡し、時点に依存する内容は as_of 基準で答えるよう指示する。
  回答言語は日本語。
- `service.py` は変更しない (dispatch と answered 組み立ては実装済み。
  エラーは自然に伝播する)。

## Invariants

- direct 経路の成功は必ず answered。失敗を成功系 status
  (answered / insufficient) に変換しない。fallback draft を作らない。
- retry / raise の対象は**明示列挙した失敗のみ**: blank 応答
  (retry 1 回 → `DirectAnswerInvalidError`) / `AIProviderError`
  (即伝播)。**想定外例外は握りつぶさず伝播する**。`except Exception` を
  書かない。
- **分類済み失敗 (`AIProviderError` / blank 全滅の
  `DirectAnswerInvalidError`) で raise する経路では**、attempt failure /
  final failed event / metric を記録してから伝播する (沈黙で死なない)。
  想定外例外はこの記録の対象外 (捕まえない・記録しない・そのまま伝播)。
  audit recorder 自体の失敗は best-effort (B-1 同様、記録失敗で工程を
  止めない)。
- 分類属性 (failure_kind / disposition) に経路名を混ぜない。
  `AIProviderError` は包まず素通し、`DirectAnswerInvalidError` は
  direct.py 所有。共通基底を作らない。
- `DirectAnswerer` port signature は変更しない。`contract.py` は
  変更しない。
- プロンプトは direct 専用。misrouting をプロンプトで防御しない。
- 秘密情報は settings 経由。プロンプトに質問・as_of 以外の内部情報を
  入れない。

## Non-goals

- API endpoint / HTTP エラーマッピング / FastAPI DI (次スライス)。
- real planner 込みの統合貫通 (internal retrieval の DB port 手当てと
  合わせて統合スライスで行う。probe は固定 `NoRetrievalPlan` で direct
  経路のみ貫通する)。
- Gemini adapter 共通処理 (client 初期化 / blocked 判定 / error translate)
  の共通化 (提案に留める)。
- プロンプトの品質チューニング・eval 整備 (動く正直な v1 まで)。
- rate limit 値の最適化 (B-1 と同等の保守的設定で開始)。
- misrouting の自動検出・planner への還元 (観測データが溜まってから)。

## Changed Files

```text
backend/app/agent/answering/direct.py               (service + generator port + typed error)
backend/app/agent/answering/ai/gemini_direct.py      (新規)
backend/app/agent/answering/ai/gemini_direct_prompt.py (新規)
backend/app/agent/answering/ai/gemini_direct_spec.py (新規)
backend/app/agent/answering/audit.py                 (direct イベント追加)
backend/app/agent/answering/metrics.py               (direct counter 追加)
backend/app/agent/answering/__init__.py              (export)
backend/scripts/probe_question_answering.py          (direct 経路モード追加)
backend/tests/agent/answering/test_direct.py         (新規)
backend/tests/agent/answering/ai/test_gemini_direct_*.py (新規)
```

## Tests

fake generator で `DirectAnswerService` を検証する。

1. 正常: text が返る → `DirectAnswerDraft` になり answered final event +
   metric が記録される (retry なし)。
2. blank (空文字・空白のみ) → previous_error 付きで 2 回目が呼ばれ、
   2 回目が valid なら採用される (retry_used が記録される)。
3. 2 回目も blank → `DirectAnswerInvalidError` が raise され、attempt
   failure 2 件 + failed final event + metric が記録済みである。
4. `AIProviderError` → retry されず即伝播し、attempt failure + failed
   final event が記録済みである。
5. 想定外例外 (分類外の Exception) → retry・記録なしでそのまま伝播する。
6. audit recorder が例外を投げても工程は失敗しない (best-effort)。
7. metrics: answered / failed が次元付きで記録される (capfire)。

adapter (Gemini) はプロンプト render (question / as_of / previous_error
埋め込み) と応答処理 (text 素通し / blocked finish_reason →
`AIProviderOutputBlockedError`) の unit を B-1 gemini テストの流儀で書く。
実 API は probe のみ。

## Probe (手動貫通)

`probe_question_answering.py` に direct 経路モードを追加する:

- planner: 固定 `NoRetrievalPlan` を返す script 内 stub (決定的にする。
  real planner だと質問により retrieval 経路が選ばれ probe が不安定になる)。
- direct_answerer: 実 Gemini (`gemini-3.1-flash-lite`) を包む
  `DirectAnswerService`。
- retriever / synthesizer: 呼ばれたら raise する stub (不到達)。
- 必要な secret は `GEMINI_API_KEY` のみ。direct mode では Tavily /
  DeepSeek の key check を行わない (retriever / synthesizer 不到達のため、
  無関係な外部検索設定に依存させない)。
- 出力: answer / status / audit (attempt_failures / final event /
  retry_used)。

成功判定は形で行う: `status="answered"` / sources 空 / missing 空の
validate 済み `AnswerQuestionResult` が返ること。回答の内容には依存しない。

## Done

- `DirectAnswerService` + Gemini direct adapter が存在し、fake generator の
  unit テストが green。
- 失敗時に typed error (`AIProviderError | DirectAnswerInvalidError`) が
  伝播し、raise 前に audit / metric が焼かれている。
- direct 工程側 (`DirectAnswerer` / `DirectAnswerService`) の docstring に
  raisable surface が明記されている。
- probe で実 Gemini を通した direct 経路の貫通が確認できる
  (実行はユーザー環境)。
- 既存 suite に regression なし。
