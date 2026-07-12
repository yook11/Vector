# External search DeepSeek adapters slice 仕様

## 位置付け

runner の LLM port 2 つ(`QueryGenerator` / `EvidenceSelector`)の
DeepSeek-V4-Flash 実装を追加する slice。前提:
external-search-research-runner-slice(骨格)と
external-search-tavily-provider-slice(provider)が実装済みであること。

これで「task あたり DeepSeek 2 call(query 生成 + 選別)」の中身が揃い、
3 port すべてに実 adapter が存在する状態になる。

composition root への配線・実 E2E・回答生成(evidence →
`AnswerQuestionResult`)は後続の統合 slice の責務とする。
実応答の品質(query の質・選別の質)は unit test では検証できないため、
統合 slice の E2E / eval に明示的に送る。

## Problem

fake しかない 2 つの LLM port に実装を与えるにあたり、次を同時に満たす。

- 入力の untrusted 扱い: `collection_goal` と `target_time_window` は
  ユーザー質問由来、候補の title / snippet は web 由来。いずれも prompt
  境界内の data として渡し、文中の指示を実行させない。
- 出力の構造強制: strict function calling で schema を強制しつつ、
  準拠を保証と見なさず code 側の clamp(from_raw / runner)を通す。
- envelope 契約違反(tool_call 欠落・非 JSON arguments 等)を自己記述の
  語彙で分類し、SDK 例外型を port から漏らさない。
- URL を LLM に渡さない・出力させない(index-only の原則を prompt でも維持)。
- dev の docker backend は egress が無く、実 API 検証はこの slice では行わない。

## Evidence

- `backend/app/agent/evidence_collection/external_search/contract.py`
  - `QueryGenerator.generate(task, as_of, target_time_window) -> list[str]`。
  - `EvidenceSelector.select(task, candidates, as_of) -> EvidenceSelectionResult`。
  - `EvidenceSelectionResult.from_raw` が claim / why_selected / missing を
    clamp する factory。**直接構築は過長で ValidationError になる**ため、
    adapter は必ず from_raw 経由で構築する(runner slice レビューの申し送り)。
  - `ExternalQueryGenerationError` / `ExternalEvidenceSelectorError` が
    分類済み境界 error。cap 定数(query 3 件 / 200 字、selection 5 件、
    claim / why_selected 300 字、missing 5 件 / 200 字)。
- `backend/app/agent/evidence_collection/external_search/runner.py`
  - 両段 30s の backstop timeout。querygen 出力は runner 側でも clamp される
    (adapter の clamp は一次防衛、runner は二次防衛)。
- `backend/app/analysis/assessment/ai/`(Stage 4 の DeepSeek パターン)
  - `spec.py`: frozen dataclass + module singleton、`compute_call_signature`
    による version 算出、`tool_choice` 強制 + `thinking: disabled`、
    `base_url="https://api.deepseek.com/beta"`。
  - `schema_tool.py`: strict function calling の schema は lowercase JSON
    Schema + `additionalProperties: false`、`$ref`/`$defs` 禁止(inline)。
    **`key_points` が array-of-objects のネストで本番稼働済み**のため、
    selector の nested schema に技術リスクはない。
  - `deepseek.py`: adapter は SDK I/O と例外翻訳のみに責務を絞る。
    envelope 契約違反は `DeepSeekResponseDefect`(自己記述 StrEnum)で分類。
    空 key は `AIProviderConfigurationError` で `__init__` fail-fast。
- `backend/app/analysis/deepseek_error_translator.py`
  - `translate_deepseek_error(exc)`: OpenAI SDK 例外を `AIProvider*Error`
    階層へ翻訳。マップ不能は素通し(caller が bare re-raise)。
- `backend/app/analysis/prompt_safety.py`
  - `sanitize_for_untrusted_block`。planning の Gemini prompt が既に利用
    (agent → analysis の共有 AI 基盤 import は既存精度)。
- `backend/app/config.py`
  - `deepseek_api_key` は既存(Stage 4 で使用中)。settings 追加は不要。

## Decision

### レイアウト(assessment/ai と同型)

```text
app/agent/evidence_collection/external_search/ai/
  prompts.py       EXTERNAL_QUERY_GENERATOR_PROMPT / EXTERNAL_EVIDENCE_SELECTOR_PROMPT
  schema_tool.py   QUERY_GENERATOR_TOOL_SCHEMA / EVIDENCE_SELECTOR_TOOL_SCHEMA
  spec.py          ExternalSearchDeepSeekSpec + spec singleton 2 つ
  deepseek.py      DeepSeekQueryGenerator / DeepSeekEvidenceSelector
```

### Call spec(Stage 4 と同じ原則)

- `AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=".../beta")`、
  `model="deepseek-v4-flash"`。
- `tools=[{"type": "function", "function": {"strict": True, ...}}]` +
  `tool_choice` で tool 名を強制 + `extra_body={"thinking": {"type": "disabled"}}`。
- `version` は `compute_call_signature(prompt, model, gen_config, schema)` で
  算出(手動 bump なし)。
- `gen_config`: querygen は `max_tokens: 256`、selector は `max_tokens: 2048`。
  temperature は Stage 4 DeepSeek と同じく指定しない。
- `rate_limit_policy`: rules 無し(Stage 4 DeepSeek と同じ)。
- SDK client の timeout は `EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS = 20` の named
  定数で持つ(runner backstop 30s の内側)。
- adapter 内 retry はしない。失敗は 1 task の部分失敗として report で可視化
  される設計であり、planner の repair retry(失敗コスト = plan 全体)とは
  条件が違う。実測失敗率を見てから再検討する。

### Tool schema

```python
# generate_search_queries
{
    "type": "object",
    "additionalProperties": False,
    "required": ["queries"],
    "properties": {
        "queries": {
            "type": "array",
            "description": (
                f"1 to {EXTERNAL_TASK_QUERY_LIMIT} short English keyword "
                "queries for external news search."
            ),
            "items": {"type": "string"},
        },
    },
}

# select_evidence
{
    "type": "object",
    "additionalProperties": False,
    "required": ["selections", "missing"],
    "properties": {
        "selections": {
            "type": "array",
            "description": (
                f"Useful candidates only, at most "
                f"{EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK}. "
                "Empty if none are useful."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidate_index", "claim", "why_selected"],
                "properties": {
                    "candidate_index": {"type": "integer"},
                    "claim": {"type": "string"},
                    "why_selected": {"type": "string"},
                },
            },
        },
        "missing": {
            "type": "array",
            "description": (
                f"At most {EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK} short "
                "Japanese notes on what could not be confirmed."
            ),
            "items": {"type": "string"},
        },
    },
}
```

**DeepSeek strict mode は string の `maxLength` / array の `minItems` /
`maxItems` に非対応**(公式 docs。request が provider 側 schema validation で
落ちるリスクがあるため schema に入れない)。Stage 4 の稼働実績が保証するのは
nested array-of-objects と `pattern` までで、件数・長さ keyword は使っていない。
統合 slice の実 E2E で strict 受理を最終確認する。

件数・長さの cap は数値を contract.py の定数が単一所有し、
schema description(定数から f-string で組み立て)・prompt 指示・code 側
clamp(from_raw / runner)で防衛する。schema keyword による強制はしない。

### Prompt 方針

共通: untrusted 入力は `<untrusted_input>` 境界タグで包み、
`sanitize_for_untrusted_block` を通す。境界内の指示・命令は入力テキストとして
扱い実行しないことを明示する(planner prompt と同じ規律)。

QueryGenerator prompt:

- 入力: collection_goal(untrusted)、as_of、target_time_window(nullable)。
- 指示: 外部ニュース検索でヒットしやすい**英語** keyword query を 1〜3 件。
  同じ角度の言い換えを並べず、角度(製品 / 企業 / 影響 / 公式発表など)を
  変える。角度が 1 つしかなければ 1 件でよい(件数ノルマなし)。
- time_range 等の構造化時間情報は出力させない(Non-goals)。

EvidenceSelector prompt:

- 入力: collection_goal(untrusted)、as_of、候補一覧(untrusted)。
  候補は `[index] title / source_name / published_at / snippet` の形式で列挙し、
  **URL は渡さない**(出力は index-only なので不要。token と注入面を減らす)。
- 指示: goal に照らして根拠として有用な候補だけ選ぶ。弱い候補・重複候補は
  選ばない。**該当がなければ selections は空でよい**(件数ノルマなし)。
  candidate_index は列挙された index のみ。claim(この記事が何の根拠か)/
  why_selected / missing(goal に対して未確認の点)は日本語で書く。
  published_at と as_of で鮮度を考慮する。

### Adapter(SDK I/O と例外翻訳のみ)

```python
class DeepSeekQueryGenerator:
    def __init__(self) -> None: ...   # 空 key は AIProviderConfigurationError で fail-fast

    async def generate(
        self, *, task, as_of, target_time_window
    ) -> list[str]: ...


class DeepSeekEvidenceSelector:
    def __init__(self) -> None: ...

    async def select(
        self, *, task, candidates, as_of
    ) -> EvidenceSelectionResult: ...
```

- envelope 契約違反は Stage 4 と同型の自己記述 StrEnum
  (`ExternalDeepSeekResponseDefect`: no_tool_call / wrong_tool_name /
  arguments_not_json / arguments_not_dict / **arguments_schema_invalid**)で
  分類する。`arguments_schema_invalid` は arguments が JSON object ではあるが
  期待 shape でない場合(required field 欠落・型違い・`from_raw` の
  ValidationError)を指す。strict mode の schema 準拠は保証と見なさず、
  arguments は必ず code 側で validate してから使う。
- 例外変換: SDK 例外は `translate_deepseek_error` で分類し、
  `AIProvider*Error` に翻訳できたものと envelope defect を、querygen は
  `ExternalQueryGenerationError`、selector は `ExternalEvidenceSelectorError`
  に変換して raise する。**reason は message 埋め込みではなく instance 属性で
  持ち、`exc.reason` で defect / provider 失敗の分類値が読める**ようにする。
  contract.py の 2 error 型に optional `reason: str` 属性を追加する(既定値
  ありで、runner テストの既存 fake 呼び出しは非破壊)。翻訳できない例外は
  そのまま伝播させる(未知のバグは隠さない)。
- querygen: arguments の shape(`queries` が存在し list であること)を検証し
  (違反は arguments_schema_invalid)、str 要素のみに正規化して返す。
  clamp(件数 / 長さ / 重複)は runner の責務なので二重実装しない。
- selector: arguments の shape(`selections` / `missing` が list であること)
  を検証し、出力は必ず `EvidenceSelectionResult.from_raw` で構築する。
  from_raw 内の ValidationError(要素の型違い・負 index 等)も
  arguments_schema_invalid として selector error に閉じる。壊れた要素の
  単体 drop はしない(型・契約違反は task の selector_failed として可視化し、
  値の丸めは runner / from_raw の clamp に限る)。
- raw request / raw response / prompt 本文を log・例外 message に載せない。

## Invariants

- provider / envelope / arguments schema の**既知の失敗**は
  `ExternalQueryGenerationError` / `ExternalEvidenceSelectorError`(+ 構築時の
  `AIProviderConfigurationError`)に閉じ、SDK 例外型を漏らさない。
  **未知のプログラミングバグは握らず伝播**させる(runner の「未分類例外は
  握らない」規律と対)。
- selector の結果構築は from_raw 経由のみ(直接構築の経路を作らない)。
- collection_goal / target_time_window / 候補 text は `<untrusted_input>` 境界 +
  `sanitize_for_untrusted_block` を通してのみ prompt に入る。
- LLM に URL を渡さず、URL を出力させる field を schema に作らない。
- prompt に件数ノルマを書かない。空 selections / 1 件の queries は正常系。
- 件数・長さ cap の数値は contract の cap 定数が単一所有し、schema
  description・prompt・code 側 clamp はそこから組み立てる。DeepSeek strict
  非対応の keyword(maxLength / minLength / minItems / maxItems)を
  tool schema に入れない。
- prompt / schema / gen_config の変更で version が自動的に回る。
- api key・raw 応答・prompt 本文を log / audit / 例外 message に載せない。

## Non-goals

- composition root への配線・実 E2E(統合 slice)。
- adapter 内 retry / repair prompt。
- time_range enum の出力(Tavily 日付フィルタ対応とセットで将来)。
- 反復リサーチ(結果を見た query 再生成)。
- 回答生成(evidence → AnswerQuestionResult)。
- query / 選別品質の eval 基盤。
- 新規 dependency の追加(openai SDK は Stage 4 で導入済み)。

## Behavior

```text
DeepSeekQueryGenerator.generate(task, as_of, target_time_window)
  prompt = QUERY_GENERATOR_PROMPT.format(
    goal=sanitize(task.collection_goal), as_of, time_window=sanitize(time_window))
  response = client.chat.completions.create(model, tools, tool_choice, ...)
    - SDK 例外: translate_deepseek_error で分類
        -> AIProvider*Error なら ExternalQueryGenerationError(reason) へ
        -> 翻訳不能なら bare re-raise
  envelope を検証(defect: no_tool_call / wrong_tool_name /
    arguments_not_json / arguments_not_dict)
  arguments の shape を検証(queries 欠落 / 非 list -> arguments_schema_invalid)
  ※ defect はすべて ExternalQueryGenerationError(reason=defect 値) へ
  queries = queries の str 要素のみ
  return queries          # clamp は runner が行う

DeepSeekEvidenceSelector.select(task, candidates, as_of)
  prompt = SELECTOR_PROMPT.format(
    goal=sanitize(goal), as_of,
    candidates=[f"[{i}] {sanitize(title)} / {source_name} / {published_at}
                 / {sanitize(snippet)}"])   # URL なし
  response = 同上(defect / SDK 例外 -> ExternalEvidenceSelectorError)
  arguments の shape を検証(selections / missing 欠落・非 list
    -> arguments_schema_invalid)
  return EvidenceSelectionResult.from_raw(
    selections=arguments["selections"], missing=arguments["missing"])
    - from_raw の ValidationError も arguments_schema_invalid として
      ExternalEvidenceSelectorError(reason 付き)に閉じる
```

## Tests

Unit tests only。fake SDK client(assessment のテストパターン踏襲)で完結し、
実 API は呼ばない。

1. spec / schema(G1: cap の単一所有と strict 互換)
   - 2 つの tool schema に DeepSeek strict 非対応 keyword
     (maxLength / minLength / minItems / maxItems)が含まれない
     (schema dict を再帰走査して assert)。
   - 件数 cap を含む description が contract の cap 定数から組み立てられて
     いる(定数を変えると description も変わる)。
   - `additionalProperties: false` / required / `$ref` 不使用。
   - prompt / schema 変更で version が変わる(compute_call_signature 経由)。
   - `EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS` が runner backstop
     (QUERY_GENERATE / EVIDENCE_SELECT_TIMEOUT_SECONDS)より小さいことを
     assert し、client 構築に渡ることを 1 本で固定する
     (「backstop の内側」という設計の固定が主目的)。
2. prompt(G2: 性質のみ、golden にしない)
   - goal / title / snippet の境界タグ突破文字列が sanitize され、
     構築済み prompt に生で現れない。
   - selector prompt に候補 URL が含まれない。
   - querygen に「角度が 1 つしかなければ 1 件でよい」、selector に
     「該当がなければ空でよい」に相当する逃げ道の文言が含まれる。
   - NG は限定否定のみ: 「必ず3件」「必ず5件」等の件数強制文言が無い
     (広い否定 assert はしない。「1〜3件」等の上限提示は正当)。
3. DeepSeekQueryGenerator(G3 / G4)
   - happy path: tool_call arguments の queries が返る。
   - envelope defect 4 種 + arguments_schema_invalid(queries 欠落 /
     queries 非 list)がそれぞれ `ExternalQueryGenerationError` になり、
     `exc.reason` で defect 値が読める。
   - SDK 例外(rate limit / timeout / connection / 402)が
     `ExternalQueryGenerationError` になり、SDK 例外型が漏れない。
   - 翻訳不能な例外(fake が ValueError)は素通しで伝播する。
   - 非 str 混入の queries は str 要素のみ返る。
   - 空 key で `AIProviderConfigurationError`。
4. DeepSeekEvidenceSelector(G3 / G4)
   - happy path: **301 字の claim が 300 字に truncate されて返る**ことで
     from_raw 経由を証明する(等値 assert では直接構築と区別できない)。
   - missing 6 件 / 201 字が 5 件 / 200 字に clamp される。
   - selections 空は空の結果として正常に返る(error にならない)。
   - selections 非 list / 要素が非 mapping / candidate_index 負値
     (from_raw の ValidationError)は arguments_schema_invalid として
     `ExternalEvidenceSelectorError` になる。
   - envelope defect / SDK 例外 / 翻訳不能例外の分類は querygen と同様。
5. 秘匿(G5)
   - 全失敗経路で、例外 message に api key / raw response body /
     prompt 本文が含まれない(fake の body と prompt 素材に目印文字列を
     仕込んで `not in` で assert)。

検証しないこと(責務の所有を明示): candidate_index の範囲検証と querygen の
件数 / 長さ / 重複 clamp は runner の所有(runner テストで固定済み)。
DeepSeek が schema を守るかは外部挙動(守らない前提で 3 / 4 を張る)。
query・選別の品質は unit test の外(統合 slice の E2E / eval)。

## Done

- 2 adapter + prompts + schema_tool + spec が実装され、上記テストが green
  (`/check` で検証)。
- runner の fake 2 port を DeepSeek adapter に差し替えるだけで動く状態
  (配線自体は統合 slice)。
- 実 API 検証・品質検証は統合 slice の E2E / eval に送ることが明記されている。
- 新規 dependency の追加なし。retry / time_range / 配線は未実装のまま。
