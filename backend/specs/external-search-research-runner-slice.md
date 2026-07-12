# External search research runner slice 仕様

## 位置付け

`ExternalSearchRunner` の実体である research runner(最大 3 並列の
ResearchTask worker)を、fake port だけで完結する骨格として実装する slice。

前提: question-answering-external-research-task-contract-slice で
`ExternalResearchTask(collection_goal)` が plan contract に入っていること。

リサーチャーの実体は「自律 tool-use agent」ではなく、goal を起点にした
直線パイプライン worker とする。

```text
query 生成 -> provider 検索(複数 query) -> 候補 pool -> 選別・注釈
```

検索 query は plan にはなく、リサーチャーが goal から実行時に生成する。
DeepSeek-V4-Flash は task あたり 2 call(QueryGenerator + EvidenceSelector)。
「検索結果を見て query を作り直す」反復は将来の runner 内部拡張とし、
v1 は 1 パスで固定する(port 境界はそのまま反復化に使える)。

実検索 provider adapter と DeepSeek adapter(query 生成 / 選別)は後続 slice の
責務とし、本 slice は worker 並列制御・上限(構造 cap)・部分失敗の可視化・
index 参照による URL 出所保証・検索内容の追跡点(report)を固める。

## Problem

runner には次を同時に満たす実行構造が必要になる。

- 最大 3 並列で task を処理しつつ、task を取りこぼさない。
- goal から生成した複数 query の候補を 1 つの pool に束ね、selector が
  複数角度の候補を見比べて選別できるようにする。
- query 生成が実行時に移ったため、「実際に何を検索したか」を report で
  追跡できるようにする(plan には query が無い)。
- LLM / provider の異常応答(過大件数・不正 index・過長文字列)で処理を
  止めず、Answerer のコンテキストと selector 入力長を守る。
- 1 task の失敗が他 task の evidence を巻き込まない。
- 「検索したが有用な根拠が無かった」「query を作れなかった」「検索が失敗した」
  を値で区別できる。
- selector(LLM)が URL を捏造・改変できない。

## Evidence

- `backend/app/agent/evidence_collection/external_search/service.py`
  - `ExternalSearchRunner` protocol、`resolve_external_search_agent_count`、
    `EXTERNAL_SEARCH_AGENT_HARD_LIMIT = 3` が既にある。
  - `ExternalSearchEvidence` は url / title / snippet / published_at /
    source_name / source_ref / query を持つ(claim / why_selected / task 対応は
    まだ無い)。
- `backend/app/agent/answering/service.py`
  - `retrieve(plan)` は as_of を受け取っていない。query 生成の時制判断と
    selector の鮮度判断には `AnswerQuestionInput.as_of` を届ける必要がある。
- `backend/app/schemas/`(`ArticleBrief.build_brief`)
  - 「clamp はファクトリで行い、model は不変条件として保持する」既存パターン
    (key_points 最大 3 件・各 250 字 cap)。本 slice の cap 設計はこれを踏襲する。
- `backend/app/shared/security/safe_url.py`
  - evidence URL は `SafeUrl` で検証される。ただし「実在するが無関係な URL」は
    型検証では防げないため、index 参照で出所を保証する。
- feedback: 故障の見える化を優先し、fallback で隠蔽しない。
  query 生成失敗時に goal 文をそのまま query に流用する、selector 失敗時に
  上位 N 件を機械採用する、等の代替は置かない。

## Decision

### 実行構造

「agent 3 体」は並列度として実装する。task ごとに 1 worker coroutine を作り、
`asyncio.Semaphore(effective_agent_count)` で同時実行数を絞る。
task 数が effective_agent_count を超えても task は削らない(並列度だけを絞る)。

worker は port 3 つの直線パイプライン。

```text
ResearchWorker(task_index, task, as_of, target_time_window)
  1. queries = query_generator.generate(task, as_of, target_time_window)
       生成結果を code 側で clamp(件数 / 長さ / 空白 / 重複)
       有効 query 0 件は query_generation_failed
  2. 各 query を並行に provider.search(query, limit=CANDIDATES_PER_QUERY)
       一部失敗は計数して継続、全 query 失敗のみ provider_failed
  3. URL dedupe(初出勝ち)+ query 間 round-robin interleave で
       pool を POOL_LIMIT 件に cap(1 query が pool を独占しない)
       pool 0 件は selector を呼ばず succeeded / evidence 0 件
  4. result = selector.select(task, pool, as_of)
  5. selection を検証(不正 index / 重複 index / 上限超過を除去・計数)
  6. pool[index] を引き直して ExternalSearchEvidence(task_index 付き)を構築
  7. ResearchTaskReport を構築(generated_queries はここに記録)
```

### 上限は構造で持つ(止めない)

module 定数(settings 化は環境ごとに変える理由が出るまでしない):

```python
EXTERNAL_TASK_QUERY_LIMIT = 3
EXTERNAL_QUERY_MAX_CHARS = 200
EXTERNAL_SEARCH_CANDIDATES_PER_QUERY = 10
EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK = 20
EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK = 5
EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK = 5
EVIDENCE_CLAIM_MAX_CHARS = 300
EVIDENCE_WHY_SELECTED_MAX_CHARS = 300
MISSING_ITEM_MAX_CHARS = 200
```

コスト上限は plan 側の task 3 件 cap(contract slice)と合わせて構造で閉じる:
1 質問あたり DeepSeek 最大 6 call(3 task × 2)、provider 最大 9 call
(3 task × 3 query)、selector 入力は task あたり pool 20 件以下。

置き場所の割当:

- query cap: DeepSeek adapter(後続 slice)の prompt / schema description が
  一次防衛(DeepSeek strict は minItems / maxItems / maxLength 非対応で
  schema keyword に焼けない)。runner の件数 clamp・
  `EXTERNAL_QUERY_MAX_CHARS` truncate・空白除去・重複除去が防衛の本線。
- candidate cap: provider port の `limit` 引数で要求時に絞るのが主防衛。
  応答が limit を超えた場合は provider rank 順(list 順)で truncate する(防御)。
- selection cap: selector adapter の prompt / schema description が一次防衛
  (同上の理由で schema keyword は使えない)。runner が超過分を先頭から
  truncate して計数するのが防衛の本線。
- 文字列長 cap: `claim` / `why_selected` / `missing` 要素は件数だけでなく
  文字数も cap する。超過は拒否ではなく truncate。clamp はファクトリで行い、
  frozen model の validator は「ファクトリを通れば違反しない」不変条件として持つ
  (経路上のバグは ValidationError で大きく見える)。

### URL 出所の構造保証

selector は URL を返さない。pool 内の `candidate_index` だけを返し、
url / title / published_at / source_name は runner が pool から引き直す。
これにより evidence の URL が常に検索 provider 由来であることを構造で保証する。

- 不正 index(範囲外): その selection だけ捨てて計数する。
- task 内の重複 index: 最初の selection を残し、以降を捨てて計数する。

### 部分失敗の可視化と検索内容の追跡

task ごとに `ResearchTaskReport` を作り、outcome に載せる。
丸め・失敗・selection 検証の結果と、実行時に生成した query は
すべてここに集約する。query は plan に無いため、
**report の `generated_queries` が「実際に何を検索したか」の SSoT** になる。

- `succeeded` で `evidence_count == 0` は「検索したが有用な根拠なし」。
  `candidate_count == 0` の場合は provider が成功したが候補が無かったことを表し、
  selector は呼ばない。
- `query_generation_failed` は「query を作れなかった」。goal 文を query に
  流用する silent fallback はしない(日本語の目的文は検索 query として機能せず、
  失敗を隠すだけになる)。
- `provider_failed` は「全 query の検索が失敗した」。一部失敗は
  `provider_failed_query_count` に計数して継続する。
- `selector_failed` 時に上位 N 件を機械採用する fallback は置かない。
- timeout は当該段の failure として分類する(独立 status にしない)。
- worker が捕捉するのは分類済み error 型と TimeoutError のみ。
  未分類の例外は握らず伝播させる(バグは静かに report 化しない)。

### task 横断の集約

`ExternalSearchService` が全 task の結果を集約する。

- 同一 URL の cross-task 重複は最初の出現(task 順 → selection 順)を残す。
  除去件数は outcome の `deduplicated_evidence_count` に計数する。
  (別 task が同じ query を生成して同じ記事に到達するのは正常な重複。)
- 意味的重複排除・ranking は行わない(Non-goals)。
- `task_index` / `source_ref` は dedupe 後も振り直さない。
- runner は `ExternalSearchOutcome` を直接返さず、`ExternalSearchRunResult`
  (`evidence` + `task_reports`)を返す。requested/effective の丸め、
  cross-task URL dedupe、final outcome 化は service の集約責務とする。
- `ExternalSearchOutcome` は model validator で `task_reports` と `tasks` の
  対応を検証する。`len(task_reports) == len(tasks)`、report の
  `task_index` は task 範囲内かつ一意、evidence の `task_index` は task 範囲内、
  `source_ref` は outcome 内で一意でなければならない。さらに
  `sum(report.evidence_count) == len(evidence) + deduplicated_evidence_count` を
  満たす必要がある。

### as_of / target_time_window の伝搬

query 生成の時制判断と selector の鮮度判断に必要なため、
次の signature 変更を行う。

```python
class ExternalPlanSearcher(Protocol):
    async def search_plan(
        self,
        plan: QuestionPlan,
        *,
        as_of: datetime,
        requested_agent_count: int | None = None,
    ) -> ExternalSearchOutcome: ...


class QuestionAnsweringService:
    async def retrieve(
        self, plan: QuestionPlan, *, as_of: datetime
    ) -> RetrievalOutcome: ...
```

`target_time_window` は plan が既に持っているため引数追加は不要。
internal retrieval の signature は変更しない。

## Invariants

- 同時に実行中の worker 数は常に `effective_agent_count`(<= 3)以下。
- task は取りこぼさない。並列度制限は処理順を遅らせるだけで対象を減らさない。
- 1 質問あたりの LLM / provider 呼び出し回数は上記定数の積で上限が閉じている。
- evidence の url / title / published_at / source_name は candidate 由来のみ。
  selector 出力からこれらの field を直接構築する経路を作らない。
- collection_goal はユーザー質問由来のテキストであり、query 生成・選別の
  LLM 入力では untrusted data として扱う(prompt 境界の設計は adapter slice)。
- selector 出力の自由記述(claim / why_selected / missing)も untrusted な
  検索結果に由来するデータとして扱い、後段で指示として解釈させない。
- 1 task の失敗(分類済み error / timeout)は他 task の結果に影響しない。
- 上限超過・不正 index・重複・過長文字列はすべて「丸めて計数して継続」。
  処理は止めない。
- `ResearchTaskReport` の status / 計数 / generated_queries だけで、task に
  何が起きて何を検索したかが値で再構成できる(log を読まないと分からない
  状態にしない)。
- selector が空 selections を返すのは正常系(空 evidence の succeeded)。
- evidence / report と task の対応は `task_index` で引く。
- raw provider 応答 / raw LLM 応答を log / audit に載せない。

## Non-goals

- 実検索 provider adapter(provider 選定は別途 /research で比較後)。
- DeepSeek adapter 2 種(QueryGenerator / EvidenceSelector)の実装。
  prompt injection 対策の prompt 設計もそちらに含める。
- 反復リサーチ(検索結果を見た query 再生成)。v1 は 1 パス。
- evidence の ranking / 意味的 dedupe / answer synthesis / citation rendering。
- metrics / logfire emission(task_reports で値としての観測を先に確立し、
  metrics 化は observability slice で行う)。
- report の pipeline_events 監査への接続。載せる場合は `generated_queries` /
  `collection_goal` / `missing` がユーザー質問由来テキストを含むため、
  PII 方針の確認をそのタイミングで行う(v1 は outcome 内のみ)。
- ページ本文 fetch。v1 は provider の snippet のみを selector 入力にする
  (SSRF 面と抽出複雑性を増やさない)。
- DB schema / API response shape / 新規 dependency の変更・追加。

## Service Contract

```python
class ExternalSearchCandidate(BaseModel):
    """検索 provider が返す候補 1 件。list 順が provider rank。"""

    model_config = ConfigDict(frozen=True)

    url: SafeUrl
    title: str = Field(min_length=1)
    snippet: str | None = None
    published_at: datetime | None = None
    source_name: str | None = None


class QueryGenerator(Protocol):
    """goal から検索 query を生成する LLM 境界(後続 slice で DeepSeek)。"""

    async def generate(
        self,
        *,
        task: ExternalResearchTask,
        as_of: datetime,
        target_time_window: str | None,
    ) -> list[str]: ...


class SearchProvider(Protocol):
    async def search(
        self, query: str, *, limit: int
    ) -> list[ExternalSearchCandidate]: ...


class EvidenceSelection(BaseModel):
    """selector が返す選別 1 件。URL は返さず index で pool を参照する。"""

    model_config = ConfigDict(frozen=True)

    candidate_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1, max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS
    )


class EvidenceSelectionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    selections: list[EvidenceSelection] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class EvidenceSelector(Protocol):
    async def select(
        self,
        *,
        task: ExternalResearchTask,
        candidates: list[ExternalSearchCandidate],
        as_of: datetime,
    ) -> EvidenceSelectionResult: ...
```

adapter が raise する分類済み error 型(SDK 例外を漏らさない境界):

```python
class ExternalQueryGenerationError(Exception): ...
class ExternalSearchProviderError(Exception): ...
class ExternalEvidenceSelectorError(Exception): ...
```

report と outcome:

```python
class ResearchTaskReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_index: int = Field(ge=0)
    collection_goal: str
    generated_queries: list[str] = Field(default_factory=list)
    status: Literal[
        "succeeded",
        "query_generation_failed",
        "provider_failed",
        "selector_failed",
    ]
    provider_failed_query_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(ge=0)      # pool cap 後の候補数
    evidence_count: int = Field(ge=0)
    dropped_selection_count: int = Field(default=0, ge=0)
    missing: list[str] = Field(default_factory=list)

    # ファクトリが missing の件数 / 各要素長を clamp してから構築する


class ExternalSearchEvidence(BaseModel):
    # query field は削除する(pool 選別では候補の親 query が task 対応を
    # 表さない)。task 対応は task_index で持つ。
    task_index: int = Field(ge=0)
    claim: str = Field(min_length=1, max_length=EVIDENCE_CLAIM_MAX_CHARS)
    why_selected: str = Field(
        min_length=1, max_length=EVIDENCE_WHY_SELECTED_MAX_CHARS
    )
    # url / title / snippet / published_at / source_name / source_ref は既存のまま


class ExternalSearchRunResult(BaseModel):
    """runner が service へ返す処理結果。集約 policy は含めない。"""

    model_config = ConfigDict(frozen=True)

    evidence: list[ExternalSearchEvidence] = Field(default_factory=list)
    task_reports: list[ResearchTaskReport] = Field(default_factory=list)


class ExternalSearchOutcome(BaseModel):
    tasks: list[ExternalResearchTask]
    evidence: list[ExternalSearchEvidence]
    task_reports: list[ResearchTaskReport]
    deduplicated_evidence_count: int = Field(default=0, ge=0)
    requested_agent_count: int | None
    effective_agent_count: int
    hard_agent_limit: int

    # validator で task_reports と tasks の対応、および source_ref 一意性を保証する
```

`source_ref` は runner が `external-{task_index}-{candidate_index}` 形式で
決定的に採番する(candidate_index は pool 内 index。outcome 内で一意)。

`ExternalSearchRequest` は service が解決済みの実行文脈として `as_of` と
`target_time_window` を持ち、runner はこれを port へ引き渡す。

timeout は port 呼び出し単位の backstop として module 定数で持つ。

```python
QUERY_GENERATE_TIMEOUT_SECONDS = 30
PROVIDER_SEARCH_TIMEOUT_SECONDS = 15
EVIDENCE_SELECT_TIMEOUT_SECONDS = 30
```

## Behavior

```text
ExternalSearchService.search_plan(plan, as_of, requested_agent_count)
  tasks = plan.external_research_tasks
  effective = resolve_external_search_agent_count(task_count=len(tasks), ...)
  tasks が空: runner を呼ばず空 outcome
  run_result = runner.search(request)   # 下記 worker 群
  evidence = run_result.evidence を cross-task URL dedupe
             (除去数を deduplicated_evidence_count へ)
  ExternalSearchOutcome(tasks, evidence, run_result.task_reports, ...)

ResearchWorker(task_index, task, as_of, target_time_window)  # Semaphore 配下
  queries = query_generator.generate(task, as_of, target_time_window)
    - ExternalQueryGenerationError / TimeoutError:
        report(status=query_generation_failed), evidence なしで終了
    - clamp: 空白除去 / strip 重複除去 / QUERY_LIMIT 件 / QUERY_MAX_CHARS
    - 有効 query 0 件: query_generation_failed(provider を呼ばない)
  候補収集(query ごとに並行):
    provider.search(query, limit=CANDIDATES_PER_QUERY)
    - ExternalSearchProviderError / TimeoutError: その query だけ失敗として
      provider_failed_query_count += 1、他 query は継続
    - 全 query 失敗: report(status=provider_failed), evidence なしで終了
  pool = URL 初出勝ち dedupe -> query 間 round-robin interleave
         -> POOL_LIMIT 件に cap
    - pool 0 件: report(status=succeeded, candidate_count=0, evidence_count=0),
      selector を呼ばず終了
  result = selector.select(task, pool, as_of)
    - ExternalEvidenceSelectorError / TimeoutError:
        report(status=selector_failed, candidate_count), evidence なしで終了
        (上位 N 件を機械採用する fallback はしない)
  selections を検証:
    - 範囲外 index: 捨てて dropped_selection_count += 1
    - task 内重複 index: 最初を残して捨て、計数
    - EVIDENCE_LIMIT 超過: 先頭から truncate し、計数
  evidence = [pool[index] + claim/why_selected + task_index + source_ref]
  report(status=succeeded, generated_queries, candidate_count,
         evidence_count, dropped, missing)
    - missing はファクトリで件数 5 / 各 200 字に clamp
```

## Tests

Unit tests only。fake query generator / fake provider / fake selector で完結し、
実 network は呼ばない。

1. 並列制御
   - task 3 件・requested 2(effective 2)のとき、3 task 全部が処理され、
     同時実行のピークが 2 を超えない(fake で並行数を記録して観測)。
2. query 生成
   - 生成 query が clamp される(4 件超 / 過長 / 空白 / strip 重複)。
   - 生成が error / 有効 0 件のとき query_generation_failed になり、
     provider が呼ばれない。
   - goal 文を query に流用する fallback が起きない。
3. 候補収集と pool
   - 3 query 中 1 query の provider 失敗は succeeded のまま
     provider_failed_query_count=1 になり、残り query の候補で継続する。
   - 全 query 失敗で provider_failed になる。
   - provider が limit 超の候補を返しても truncate される。
   - pool が URL 初出勝ちで dedupe され、round-robin interleave で
     単一 query の候補が pool を独占せず、POOL_LIMIT に cap される。
4. 部分失敗の隔離
   - 1 task が失敗(query 生成 / provider / selector いずれでも)しても
     他 task の evidence は返る。
   - 未分類例外(fake が ValueError を raise)は握られず伝播する。
5. selection 検証
   - 範囲外 index / task 内重複 index が捨てられ計数される。
   - EVIDENCE_LIMIT 超過が truncate され計数される。
   - 全 selection が不正でも status=succeeded / evidence 0 件で継続する。
   - selections=[] は succeeded / evidence 0 件(失敗と値で区別できる)。
6. 構造 cap
   - claim / why_selected / missing の長さ・件数 clamp
     (ファクトリ経由は truncate、model 直接構築の超過は ValidationError)。
7. 集約
   - cross-task の同一 URL evidence が最初の出現だけ残り、
     deduplicated_evidence_count に計数される。
   - source_ref が outcome 内で一意で、evidence.task_index が
     task_reports の task_index と対応する。
8. 伝搬
   - `retrieve(plan, as_of=...)` の as_of が query generator と selector に届く。
   - plan の target_time_window が query generator に届く。
   - report の generated_queries が実際に provider へ渡った query と一致する。

## Done

- 最大 3 並列・task 非取りこぼしの worker 骨格が fake port で動き、
  上記テストが green。
- query 生成 -> pool -> 選別の 1 パスが port 3 つで固定され、
  1 質問あたりの呼び出し上限が定数の積で閉じている。
- 上限(件数 + 文字列長)がファクトリ clamp + model 不変条件として実装され、
  異常応答で処理が止まらない。
- URL が pool 経由でしか evidence に入らないことが構造で保証されている。
- ResearchTaskReport で「根拠なし」「query 生成失敗」「検索失敗」「選別失敗」が
  値で区別でき、generated_queries で検索内容が追跡できる。
- provider / DeepSeek adapter は未実装のまま(port と error 型のみ定義)。
