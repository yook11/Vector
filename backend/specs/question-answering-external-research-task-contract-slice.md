# Question answering external research task contract slice 仕様

## 位置付け

外部検索の plan 単位を「文字列 query」から「調べるべきこと(collection_goal)」へ
置き換える plan contract slice。

検索 query は plan に持たせない。query はリサーチャー
(external-search-research-runner-slice の QueryGenerator)が実行時に生成し、
実際に使った query は runner 側の `ResearchTaskReport` に記録して追跡する。
planner の責務は「外部で何を調べるべきか」の言語化に限定され、
planner prompt から keyword query 作成の指示は消える。

external-search-agent-orchestration-slice の
「DeepSeek-V4-Flash 後続 slice 方針」節はこの系列
(本 slice + external-search-research-runner-slice)で更新する。

この slice は plan contract の置き換えと planner / external search service の
機械的追従のみを扱う。query 生成・worker 設計・検証設計・adapter は
後続 slice の責務とする。

## Problem

現在の `QuestionPlan.external_queries` には 2 つの問題がある。

- 文字列 query だけでは、後続の evidence selector に「どんな情報を集めるべきか」
  「何が未確認なら missing とすべきか」が伝わらない。
- query を plan contract に固定すると、query の所有権が planner に残る。
  将来「検索結果を見て query を作り直す」反復リサーチへ進む場合、query の
  所有権はどのみちリサーチャー側に移るため、plan contract
  (planner prompt / Gemini schema / validator / テストが連動する最も変更コストの
  高い境界)を再改訂することになる。

plan を「調べるべきこと」だけにすれば、リサーチ手法の進化(query 再生成・反復)
は runner 内部の変更で完結し、planner は無傷で済む。

「実際に何を検索したか」の追跡は plan ではなく、query 生成を所有する runner の
report が持つ(観測は決定を所有する者が出す)。

## Evidence

- `backend/app/agent/contract.py`
  - `QuestionPlan.external_queries: list[str]`。`from_draft` が空白 query を除去し、
    external 系 mode で空になった場合は `fallback_query` で補う。
  - `safe_fallback()` は internal mode 固定であり、external task を持たない。
- `backend/app/agent/planning/plan_draft.py`
  - `QuestionPlanDraft.external_queries: list[str]`。
- `backend/app/agent/planning/ai/schema_tool.py`
  - `QUESTION_PLANNER_GEMINI_SCHEMA` に `external_queries`(STRING ARRAY)がある。
    置き換え後も STRING ARRAY のままで済み、nested object 化は不要。
- `backend/app/agent/planning/ai/prompts.py`
  - `# external_queries` 節で英語 keyword query の作り方を指示している。
    この指示は runner slice の QueryGenerator prompt へ移る。
- `backend/app/agent/planning/ai/gemini_spec.py`
  - `version` は `compute_call_signature(prompt, schema, ...)` で算出されるため、
    prompt / schema 変更で prompt_version は自動で回る。手動 bump は不要。
- `backend/app/agent/planning/audit.py` / `service.py`
  - `PlannerFinalEvent` は `external_query_count` を焼いている。audit payload の
    field 名変更は audit 側 schema へ波及するため名前は維持し、意味を
    「external research task 数」として test で固定する。
- `backend/app/agent/external_search/service.py`
  - `ExternalSearchService.search_plan` が `plan.external_queries` を消費している。
- 既存テスト
  - `tests/agent/test_contract.py`
  - `tests/agent/planning/test_planner.py`
  - `tests/agent/planning/ai/test_gemini_question_planner.py`
  - `tests/agent/external_search/test_service.py`
  - `tests/agent/answering/test_service.py`

## Decision

`ExternalResearchTask` を `app/agent/contract.py` に追加し、
`QuestionPlan.external_queries` を `external_research_tasks` に置き換える。

```python
EXTERNAL_RESEARCH_TASK_LIMIT = 3


class ExternalResearchTask(BaseModel):
    """外部リサーチの実行単位。planner は「何を調べるべきか」だけを言語化する。"""

    # str_strip_whitespace で strip 正規化してから min_length を検証する。
    # " " のような空白のみ入力は strip 後に "" となり ValidationError になる。
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    collection_goal: str = Field(min_length=1)
```

- `collection_goal`: この task で何を確認したいか・何が根拠として有用かを表す
  短い日本語。runner slice で query 生成と evidence 選別の基準になる。
- query field は持たない。検索 query は実行時にリサーチャーが生成する。
- 単一 field でも model にするのは、strip / 非空白の保証を型で持ち回り、
  worker / report / evidence が task 単位で参照する実行単位だからである。

`EXTERNAL_RESEARCH_TASK_LIMIT = 3` はコスト上限の前提となる構造 cap。
runner slice の「task あたり DeepSeek 2 call・provider 最大 3 call」と合わせて
1 質問あたりの上限(DeepSeek 6 call / provider 9 call)を構造で閉じる。

planner draft は goal 文字列の list とし、Gemini schema は STRING ARRAY を保つ。

`from_draft` の整形規則(goal を strip してから判定する):

- strip 後の goal が空の要素は除去する。
- strip 後に同一の goal は重複として除去する(最初を残す)。
- `EXTERNAL_RESEARCH_TASK_LIMIT` を超える分は先頭から 3 件に clamp する。
- external 系 mode で有効 goal が 0 件の場合は
  `ExternalResearchTask(collection_goal=fallback_query)` 1 件に fallback する。
  汎用定型文ではなく質問文そのものを goal にする。QueryGenerator への入力が
  goal のみであり、定型文では query 生成の手がかりが渡らないため。
  (これに伴い旧仕様の `DEFAULT_EXTERNAL_COLLECTION_GOAL` 定数は廃止する。)

`ExternalSearchService` / `ExternalSearchRequest` / `ExternalSearchOutcome` は
`queries: list[str]` を `tasks: list[ExternalResearchTask]` に置き換える機械的
追従のみ行う。`resolve_external_search_agent_count` は task 数を入力とする以外
変えない。

## Invariants

- `QuestionPlan.external_research_tasks` が「外部で何を調べるべきか」の SSoT。
  実行側は goal から query を生成するが、goal の文言自体は作り替えない。
- 完成済み task の collection_goal は strip 正規化済みかつ非空白
  (空白のみは model レベルで ValidationError)。
- 完成済み plan 内で collection_goal は一意。
- 完成済み plan の external task は最大 `EXTERNAL_RESEARCH_TASK_LIMIT` 件
  (from_draft の clamp に加えて `QuestionPlan` validator でも強制する)。
- mode 制約は従来と同型:
  - `none` / `internal`: `external_research_tasks` は空。
  - `external` / `internal_and_external`: `external_research_tasks` は 1 件以上。
- planner は検索 query・agent 起動数・並列度・evidence 件数上限を決めない。
- planner audit の `external_query_count` は external research task 数と同値。
  audit payload の field 名・型は変えない。
- `safe_fallback()` は internal mode のままとし、external task を生成しない。

## Non-goals

- QueryGenerator / worker 設計・検証設計・task report
  (external-search-research-runner-slice)。
- 実検索 provider / DeepSeek adapter の実装。
- plan への query field の復活。反復リサーチ(結果を見た query 再生成)も
  runner 内部の将来責務であり、plan contract には入れない。
- `must_find` / score / confidence / `max_evidence` 等の追加。
- audit payload field(`external_query_count`)の改名。
- DB schema / API response shape / 新規 dependency の変更・追加。

## Service Contract

```python
class QuestionPlanDraft(BaseModel):
    retrieval_mode: RetrievalMode
    internal_queries: list[str] = Field(default_factory=list)
    external_collection_goals: list[str] = Field(default_factory=list)
    target_time_window: str | None = None
    reason: str = Field(min_length=1)
```

Gemini schema の置き換え:

```python
"external_collection_goals": {
    "type": "ARRAY",
    "description": (
        "External research goals describing what evidence to collect. "
        "Short Japanese sentences. Return at most 3 items."
    ),
    "items": {
        "type": "STRING",
        "description": "One research goal for external news search.",
    },
},
```

prompt は `# external_queries` 節を `# external_collection_goals` 節へ置き換える。
keyword query の作成指示は削除し、「その調査で何を確認したいか(何が根拠として
有用か)を短い日本語で 1〜3 件書く」ことを例付きで指示する。

例:

```text
ユーザー: 直近のNVIDIAの動きと投資への影響を教えて
external_collection_goals:
- NVIDIA の直近の発表・提携・業績に関する報道を確認する
- NVIDIA 製品の供給・需要の変化が投資判断に与える影響を確認する
```

## Behavior

```text
QuestionPlan.from_draft(draft, fallback_query)
  external 系 mode:
    goals = [strip 後に非空の goal だけ残す]
    goals = 重複除去(最初を残す)して先頭 3 件に clamp
    goals が空なら [fallback_query] を goal にした task 1 件
    tasks = [ExternalResearchTask(collection_goal=g) for g in goals]

ExternalSearchService.search_plan(plan, ...)
  tasks = plan.external_research_tasks   # 完成済みなので再整形しない
  effective_agent_count = resolve(task_count=len(tasks), requested_agent_count)
  request = ExternalSearchRequest(tasks=tasks, ...)
```

## Tests

Unit tests only。実 network / Gemini API は呼ばない。

1. `ExternalResearchTask` / `QuestionPlan`
   - 空白のみ(`" "`)の collection_goal は ValidationError
     (min_length だけでは通る入力を strip 正規化で弾くことを固定)。
   - `" 目的 "` が `"目的"` に正規化される。
   - plan 内の goal 重複は ValidationError。
   - 4 件以上の task を持つ完成済み plan は ValidationError。
   - mode 制約(none/internal は task 空、external 系は 1 件以上)。
2. `QuestionPlan.from_draft`
   - 空白 goal が除去される。
   - strip 後に同一 goal は最初の 1 件だけ残る。
   - 4 件以上の goal は先頭 3 件に clamp される。
   - external 系 mode で有効 goal 0 件のとき fallback task 1 件になり、
     その collection_goal は fallback_query そのもの。
3. Gemini planner adapter
   - STRING ARRAY の応答が draft に parse される。
   - schema に `external_collection_goals` が含まれ `external_queries` が無い。
4. `ExternalSearchService`
   - task 数で effective_agent_count が決まる(丸め規則は従来と同じ)。
   - request に task がそのまま(goal 文言不変で)渡る。
5. planner audit
   - `PlannerFinalEvent` の `external_query_count ==
     len(plan.external_research_tasks)` を planner service test で固定する
     (field 名を維持したまま意味がズレるのを防ぐ)。
6. 既存テストの追従
   - `external_queries` を参照する既存テストを task 形へ更新する。

## Done

- `ExternalResearchTask`(collection_goal のみ)が contract に入り、
  `QuestionPlan` / `QuestionPlanDraft` / Gemini schema / prompt /
  `ExternalSearchService` が task 形へ揃っている。
- from_draft の除去・重複排除・3 件 clamp・fallback 規則が実装とテストで
  固定されている。
- planner prompt から keyword query 作成指示が消えている。
- planner audit の `external_query_count` の意味(= task 数)がテストで
  固定されている。
- prompt_version が schema / prompt 変更で自動的に回っている(手動 bump なし)。
- この slice では query 生成・runner・adapter・DB / API を変更していない。
