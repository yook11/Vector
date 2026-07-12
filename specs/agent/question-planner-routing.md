# Question Planner / Routing Spec

Status: Draft
Created: 2026-06-28
Scope: LLM planner が検索要否と検索先を判断する仕様

## Problem

ユーザーの自然言語リクエストを受け取り、回答前に次のどの情報取得が必要かを LLM planner が判断できるようにする。

- 検索不要
- 内部記事検索が必要
- 外部ニュース検索が必要
- 内部記事検索と外部ニュース検索の両方が必要

この仕様では planner / planning service の判断を定義する。query embedding、internal retrieval、external search、answer synthesis、API endpoint は実装対象外。

## Decisions

- UI で外部検索の切り替えは提供しない。検索要否は agent が判断する。
- LLM adapter は `QuestionPlanDraft` を返し、`QuestionPlanningFlow` が完成済み `QuestionPlan` にする。
- Planner の public contract は完成済み `QuestionPlan` を返すこと。
- `QuestionPlanDraft` は LLM 境界なので、軽微な mode/query 不整合を hard fail しすぎない。
- `QuestionPlan` は agent 内部で使う完成済み plan として mode/query の整合性を保証する。
- `none` は検索不要の会話・操作・変換に限定する。
- ニュース、企業、投資判断、最新性、日付相対表現を含む事実質問は `none` にしない。迷ったら検索側へ倒す。
- 外部検索はまだ未実装なので、`external` / `internal_and_external` は実行時に安全な `insufficient` fallback へ落とす。
- Planner の判断は初期 retrieval proposal であり、後続の Evidence Grader は取得結果を見て sufficiency を判定する。
- Planner が必要だと判断した情報取得と、実際に実行した経路は別の構造で保持する。`route="direct"` だけで外部不足を表現しない。

## Planner Input

```python
class AnswerQuestionInput(BaseModel):
    question: str
    as_of: datetime
```

`question` はユーザーの自然言語リクエストをそのまま渡す。

`as_of` は backend が request 開始時に設定する実行基準時刻である。「今日」「直近」「今週」「最新」などの相対表現を解釈する基準にする。

## Planner Draft Output

```python
class QuestionPlanDraft(BaseModel):
    retrieval_mode: Literal[
        "none",
        "internal",
        "external",
        "internal_and_external",
    ]
    internal_queries: list[str] = Field(default_factory=list)
    external_queries: list[str] = Field(default_factory=list)
    target_time_window: str | None = None
    reason: str
```

`retrieval_mode` が planner の主判断である。

`internal_queries` は内部記事検索で embedding する query 群。ユーザー入力をそのまま入れるのではなく、内部記事を探すために必要な entity / topic / event / time intent を抽出・圧縮した検索文にする。`retrieval_mode` が `internal` または `internal_and_external` の場合は原則 1 件以上入れる。ただし LLM 出力境界なので、空でも draft validation では落とさず `QuestionPlan.from_draft()` で補正する。

`external_queries` は外部ニュース検索に使う query 群。`retrieval_mode` が `external` または `internal_and_external` の場合は原則 1 件以上入れる。ただし空でも draft validation では落とさず `QuestionPlan.from_draft()` で補正する。

`target_time_window` は質問内の時間軸を planner が抽出できた場合に入れる。厳密な date range 型にはまだしない。

`reason` は planner の短い判断理由。UI には出さず、debug / test で使う。通常ログには raw question と同様に出さない。

## Retrieval Modes

### none

検索不要。agent が会話・操作・変換として直接返答してよい。

例:

- `こんにちは`
- `この機能は何ができますか？`
- `さっきの回答を短くして`
- `この回答を箇条書きにして`

禁止:

- ニュース、企業、製品、投資、株価、規制、セキュリティ、研究発表などの事実確認。
- `今日`, `昨日`, `直近`, `今週`, `最新`, `発表`, `ニュース`, `動向` など鮮度を示す質問。
- Vector 内の記事に基づく回答を求める質問。

### internal

Vector 内部の分析済み記事を検索して回答すべき質問。

例:

- `保存済みの記事から、最近のAI半導体ニュースをまとめて`
- `Vectorで見えているOpenAI関連の傾向は？`
- `内部記事をもとに、この1週間のロボティクス関連トピックを整理して`

### external

外部ニュース検索が主に必要な質問。

例:

- `今日のNVIDIAの発表は？`
- `今朝出たOpenAIのニュースを調べて`
- `直近24時間のApple関連ニュースは？`

この phase では外部検索を実装しないため、実行時は `insufficient` fallback にする。

### internal_and_external

内部記事の文脈と外部の最新確認の両方が必要な質問。

例:

- `Vectorにある過去記事も踏まえて、直近のNVIDIAの動きを教えて`
- `内部記事と最新ニュースを合わせて、AI半導体トレンドを整理して`
- `これまでの規制関連記事と今日の発表を合わせて見解を出して`

この phase では外部検索を実装しないため、実行時は外部不足を明示する `insufficient` fallback にする。内部検索だけで断定してはいけない。

## Planning Semantics

LLM adapter は初期 retrieval proposal である `QuestionPlanDraft` を作る。`QuestionPlanningFlow` は retry / fallback / audit / metrics を扱い、agent 内部が使える完成済み `QuestionPlan` を返す。最終回答の `status` は取得結果を見た後に決める。

`retrieval_mode` は「必要だと判断した情報取得」であり、`AnswerExecutionSummary.route` は「実際に実行した経路」である。この 2 つを混同しない。

責務分担:

- LLM adapter: 検索不要 / 内部 / 外部 / 両方の初期判断を draft として出す。
- QuestionPlanningFlow: draft を完成済み `QuestionPlan` にし、planner failure の retry / fallback / audit / metrics を扱う。
- Evidence Grader: 取得済み source で回答可能かを判定し、必要なら `insufficient` に落とす。

Planner と Evidence Grader が矛盾した場合、source なしで断定する方向には倒さない。安全側は `internal` retrieval または `insufficient` である。

## Contract Updates Required

`none` / `internal_and_external` を自然に表すため、実装前に Agent Core Contract を更新する。

既存 `QuestionPlan` からの変更:

- `retrieval_mode` を追加する。
- `internal_queries` を追加する。内部ベクトル検索で embedding する query 群として扱う。
- `needs_fresh_external_news` は planner output から外す。鮮度要求は `retrieval_mode`, `external_queries`, `target_time_window` で表す。
- `reason` を追加する。

Planner の proposal と実行結果を分離するため、final result に retrieval requirement summary を追加する。

```python
class AnswerRetrievalSummary(BaseModel):
    planned_mode: Literal[
        "none",
        "internal",
        "external",
        "internal_and_external",
    ]
    unmet_requirements: list[
        Literal["internal_retrieval", "external_search"]
    ] = Field(default_factory=list)
```

`planned_mode` は planner が必要だと判断した情報取得である。

`unmet_requirements` は、必要だったが実行できなかった情報取得を machine-readable に保持する。外部検索未実装の phase では `external_search` が主な値になる。

```python
class AnswerExecutionSummary(BaseModel):
    route: Literal[
        "direct",
        "internal",
        "external_search",
        "internal_and_external",
        "workers",
    ]
    used_internal_retrieval: bool
    used_external_search: bool
```

`route` は実際に実行した経路であり、`planned_mode` ではない。

派生不変条件:

- `route="direct"`: `used_internal_retrieval=False`, `used_external_search=False`
- `route="internal"`: `used_internal_retrieval=True`, `used_external_search=False`
- `route="external_search"`: `used_internal_retrieval=False`, `used_external_search=True`
- `route="internal_and_external"`: `used_internal_retrieval=True`, `used_external_search=True`
- `route="workers"`: worker 内で内部 / 外部を使いうるため、bool の組み合わせは固定しない。

`AnswerQuestionResult` の source invariant も更新する。

- `route="direct"` かつ `status="answered"` は `sources=[]` を許容する。
- `route!="direct"` かつ `status="answered"` は source を必須にする。
- `used_external_search=True` かつ `status="answered"` の場合は `ExternalUrlSource` を必須にする。
- `status="answered"` の場合は `missing_aspects=[]` を必須にする。
- `status="insufficient"` は `sources=[]` を許容するが、`answer` は必須。
- `status="answered"` の場合、`retrieval.unmet_requirements=[]` を必須にする。
- 外部検索未実装 fallback は `retrieval.unmet_requirements=["external_search"]` で表す。

## External-Unavailable Fallback

この phase では外部検索を実装しない。

`retrieval_mode="external"` の場合:

- external search は実行しない。
- `AnswerQuestionResult.status="insufficient"` を返す。
- `retrieval.planned_mode="external"` とする。
- `retrieval.unmet_requirements=["external_search"]` とする。
- `execution.route="external_search"` にはしない。外部検索を実行していないため。
- 実行経路は `execution.route="direct"`, `used_internal_retrieval=False`, `used_external_search=False` とする。
- `missing_aspects` に `外部ニュース検索` や `最新情報の確認` を入れる。
- `answer` には「この質問には外部最新情報の確認が必要です」と明示する。

`retrieval_mode="internal_and_external"` の場合:

- 内部検索を実装済みなら内部候補を取得してもよい。
- ただし外部確認が必要な質問として扱い、外部なしで `answered` にしない。
- `retrieval.planned_mode="internal_and_external"` とする。
- `retrieval.unmet_requirements=["external_search"]` とする。
- 内部検索を実行した場合は `execution.route="internal"`, `used_internal_retrieval=True`, `used_external_search=False` とする。
- 内部検索もまだ実行しない段階では `execution.route="direct"`, `used_internal_retrieval=False`, `used_external_search=False` とする。
- `status="insufficient"` とし、内部で確認できた範囲と外部不足を分けて説明する。

## Invalid Planner Output Handling

Planner LLM の structured output が schema validation に失敗した場合:

1. validation error を渡して 1 回だけ repair retry する。
2. それでも失敗したら safe fallback へ倒す。

Safe fallback:

- ニュース / 企業 / 投資 / 最新性を含む可能性がある質問: `internal` へ倒す。外部が明らかに必要なら `insufficient`。
- 挨拶 / アプリ使い方 / 変換依頼だと deterministic に判定できる質問: `none` へ倒してよい。
- 迷う場合: `none` にはしない。

fallback の既定は `internal` とする。`none` は whitelist 的に扱い、planner が壊れたときに広く使わない。

`retrieval_mode` と query field の不整合は `QuestionPlanDraft` validation では落とさない。完成済み `QuestionPlan` では不整合を許容しない。

補正例:

- `retrieval_mode="internal"` で `internal_queries=[]`: `question` を fallback query にする。
- `retrieval_mode="external"` で `external_queries=[]`: `question` を fallback external query にする。
- `retrieval_mode="none"` で query が入っている: query は無視する。

## Prompt Requirements

Planner prompt は次を明示する。

- あなたは回答生成ではなく retrieval planning だけを行う。
- ユーザーに見せる回答文は作らない。
- `none` は検索不要な会話・操作・変換に限定する。
- ニュース、企業、投資、規制、研究発表、最新性を含む質問は `none` にしない。
- `as_of` を基準に相対時間表現を解釈する。
- 内部記事を明示的に求める質問は `internal` または `internal_and_external` にする。
- 最新外部ニュースを明示的に求める質問は `external` または `internal_and_external` にする。

## Test Plan

Unit:

- 挨拶は `retrieval_mode="none"`。
- 使い方説明は `retrieval_mode="none"`。
- 回答の言い換え依頼は `retrieval_mode="none"`。
- `今日のNVIDIAの発表は？` は `external`。
- `保存済みの記事からAI半導体ニュースをまとめて` は `internal`。
- `内部記事と最新ニュースを合わせて整理して` は `internal_and_external`。
- ニュース / 企業 / 投資 / 最新性を含む質問は `none` にならない。
- `QuestionPlan.from_draft()` は `none` の query を捨てる。
- `QuestionPlan.from_draft()` は `internal_queries=[]` の `internal` draft に `question` を fallback query として入れる。
- `QuestionPlan.from_draft()` は `external_queries=[]` の `external` draft に `question` を fallback query として入れる。
- invalid structured output は 1 回 repair retry し、失敗後 safe fallback に倒す。
- external 未実装時、`external` / `internal_and_external` は `answered` にならない。
- external 未実装時、`external` は `retrieval.planned_mode="external"` かつ `retrieval.unmet_requirements=["external_search"]` になる。
- external 未実装時、`internal_and_external` は `retrieval.planned_mode="internal_and_external"` かつ `retrieval.unmet_requirements=["external_search"]` になる。
- `route` は実際に実行した経路を表し、`planned_mode` と混同されない。

Tests must not call real LLM or external APIs. Planner LLM は fake / stub で置き換える。

## Non-goals

- external search provider の選定。
- external search 実行。
- query embedding 実行。
- internal retrieval 実行。
- answer synthesis。
- API endpoint / frontend response shape。
- LangGraph dependency 追加。
- worker 実装。

## Next

1. Agent Core Contract を `direct`, `used_internal_retrieval`, `AnswerRetrievalSummary` 対応に更新する。
2. `QuestionPlan.retrieval_mode` を domain model に追加する。
3. Planner LLM interface を作る。
4. fake draft generator で planning service unit test を作る。
5. external 未実装 fallback を通す。
