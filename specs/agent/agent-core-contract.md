# Agent Core Contract

Status: Draft
Created: 2026-06-27
Scope: 今回合意した agent core の薄い入出力 contract のみ

## Problem

ユーザーの自然言語質問を受け取り、内部記事と必要に応じた外部ニュース検索を使って、根拠付きの日本語回答を返す agent core の最小 contract を定義する。

ここでは API endpoint、frontend response shape、streaming protocol、LangGraph の詳細 state はまだ固定しない。

## Decisions

- 最初の実装は non-streaming の一括応答にする。
- `AnswerQuestionResult` は agent core の final result として扱う。
- 将来 streaming 化しても、最後に確定する `completed` event の payload として `AnswerQuestionResult` を再利用できる形にする。
- UI で外部検索の切り替えは提供しない。
- 外部検索が必要かどうかは、agent が質問と内部検索結果を見て判断する。
- `status` は confidence ではなく、取得した根拠で断定可能かを表す sufficiency 判定にする。
- LLM に数値 confidence を自己申告させる設計は Phase 1 では採用しない。
- `answered` と非空の `missing_aspects` は矛盾として扱い、final result では許容しない。
- `used_external_search` は「外部検索 step を実行したか」を表す。外部検索を実行したが引用可能な source が見つからず `insufficient` になるケースは許容する。

## Input

```python
class AnswerQuestionInput(BaseModel):
    question: str
    as_of: datetime
```

`question` はユーザーの自然言語質問をそのまま受ける。

`as_of` は backend が request 開始時に設定する実行基準時刻である。ユーザーが指定した対象日時ではなく、「今日」「直近」「今週」「最新」などの相対表現を agent が安定して解釈するために使う。

`search_policy` / `search_scope` は input に含めない。外部検索の要否は agent 内部の planning / evidence grading で判断する。

## Internal Planning

`AnswerQuestionInput` を受け取った後、agent 内部で質問を計画に変換する。

```python
class QuestionPlanDraft(BaseModel):
    retrieval_mode: Literal["none", "internal", "external", "internal_and_external"]
    internal_queries: list[str] = Field(default_factory=list)
    external_queries: list[str] = Field(default_factory=list)
    target_time_window: str | None = None
    reason: str

class QuestionPlan(BaseModel):
    retrieval_mode: Literal["none", "internal", "external", "internal_and_external"]
    internal_queries: list[str] = Field(default_factory=list)
    external_queries: list[str] = Field(default_factory=list)
    target_time_window: str | None = None
    reason: str
```

「最新情報が必要か」「外部検索すべきか」「どの query を使うか」は request で渡さず、agent が `question` と `as_of` から判断する。LLM adapter は緩い `QuestionPlanDraft` を返し、`QuestionPlanningService` が完成済み `QuestionPlan` にする。agent 内部の downstream は完成済み `QuestionPlan` だけを使う。

## Output

```python
class AnswerQuestionResult(BaseModel):
    status: Literal["answered", "insufficient"]
    answer: str
    sources: list[AnswerSource]
    missing_aspects: list[str]
    execution: AnswerExecutionSummary
```

`answer` はチャット本文として表示できる自然文である。

`status` は「自信度」ではなく、「取得した根拠で断定可能な回答を作ってよいか」を表す。

- `answered`: 根拠付きで回答できた。
- `insufficient`: 根拠不足、鮮度不足、または未解決の矛盾があり、断定できない。

`insufficient` の場合でも、`answer` は空にしない。確認できた範囲、不足している情報、断定できない理由をユーザーに返せる文章にする。

`answered` の場合、`missing_aspects` は空でなければならない。

## Execution Summary

```python
class AnswerExecutionSummary(BaseModel):
    route: Literal["internal", "external_search", "workers"]
    used_external_search: bool
```

`route` は回答生成で使った主経路を表す。

- `internal`: 内部記事だけで回答した。
- `external_search`: 外部検索も使った。
- `workers`: worker / subtask 分解まで使った。

`used_external_search` は `route` と重複しうるが、将来 `workers` の中で外部検索を使う可能性があるため残す。これは「外部検索 step を実行したか」であり、「外部 source を回答根拠として採用したか」ではない。

## Sources

`AnswerSource` は回答の根拠を表す。内部的には discriminated union にし、内部記事と外部 URL の必須 field を分ける。

```python
class InternalArticleSource(BaseModel):
    kind: Literal["internal_article"]
    source_ref: str
    article_id: int
    title: str
    snippet: str | None
    published_at: datetime | None
    source_name: str | None


class ExternalUrlSource(BaseModel):
    kind: Literal["external_url"]
    source_ref: str
    url: SafeUrl
    title: str
    snippet: str | None
    published_at: datetime | None
    source_name: str | None
```

`source_ref` は `source_1`, `source_2` のような回答内引用用 ID。将来、本文中の引用と source list / card を対応させるために使う。

`ExternalUrlSource.url` は既存の `SafeUrl` を使い、検証済み HTTP/HTTPS URL として保持する。実フェッチ時の SSRF 防御は別途 safe HTTP 境界で行うが、final result に危険な URL 形を残さないための defense-in-depth とする。

## Missing Aspects

```python
missing_aspects: list[str]
```

`missing_aspects` は「何が足りなくて断定できないのか」を短い日本語ラベルで保持する。

用途:

- agent の次アクション判断。外部検索 query や worker task の材料にする。
- ユーザーへの説明。`insufficient` のときに不足している情報として表示する。
- 検証・改善。検索不足、鮮度不足、根拠矛盾などを後で確認できるようにする。

外部検索を実行しても引用可能な source が得られなかった場合は、`status="insufficient"`, `execution.route="external_search"`, `execution.used_external_search=True`, `sources=[]` を許容し、`missing_aspects` に「引用可能な外部ニュース」のような不足理由を入れる。

## Chat Presentation

ユーザーに `AnswerQuestionResult` 全体を JSON としてそのまま表示しない。

chat UI は主に次を表示する。

- `answer`
- `sources` から作る根拠リンク / 根拠カード
- `status="insufficient"` の場合は必要に応じて `missing_aspects`

`execution.route` や `used_external_search` は基本的に直接表示しない。表示する場合も `"external_search"` ではなく、「外部ニュースも確認しました」のような自然な文言に変換する。

## Minimal Flow

```text
User question
  -> AnswerQuestionInput(question, as_of)
  -> agent planning / internal retrieval / evidence grading
  -> optional external search / optional workers
  -> AnswerQuestionResult
  -> chat UI renders answer + sources + optional missing aspects
```

## Non-goals

- API endpoint の命名を確定しない。
- FastAPI schema / OpenAPI response shape を実装しない。
- frontend types を生成しない。
- streaming event schema を実装しない。
- LangGraph dependency を追加しない。
- 外部検索 provider / API key / SDK を追加しない。

## Next

1. この contract をもとに agent core の domain model / protocol を実装する。
2. `internal` route の最小貫通を作る。
3. Evidence Grader で `answered` / `insufficient` と `missing_aspects` を判定する。
4. その後、API endpoint と frontend-facing response shape を設計する。
