# Agent Research Checkpoint Context slice 仕様

更新日: 2026-08-03

実装状況: Implemented

## 位置付け

回答Runで実行した外部検索の記録を`ResearchCheckpoint`として保存し、同じthreadの後続質問では
直近の記録をPlannerへ渡す。`QuestionContext`は今回の質問の意味・要件(需要側)、本sliceは
過去に何を検索し何が得られ何が未確認か(供給側)を担当する。

新しいLLM呼び出しは追加しない。Checkpointは既存工程の成果物からの決定的な詰め替えで作り、
関連性の判断は既存Plannerの仕事(何を検索すべきかの判断)に含める。

## Work Definition

### Problem

- 現行履歴からは、過去Runの調査目的、実行query、得られた情報、未確認だった点を再構成できない。
- 後続Runが同じ検索を繰り返したり、前回の未確認事項を調査計画へ引き継げない。

### Evidence

- 調査目的の正本は`ResearchTask.research_goal`。draft→plan正規化(strip・重複除去)は
  planning contractに既存。
- 実行queryは`Researcher`が生成・実行するが、query単位のprovider成否はresearcher内部で
  件数に縮約され現在は外へ出ない(配管追加が必要)。
- 得られた情報の正本はEvidence Reviewerのselections(`claim`)、未確認事項の正本は
  Reviewerの`missing`(Run全体で1本)。
- 上限の正本は`RESEARCH_TASK_LIMIT`、`EXTERNAL_TASK_QUERY_LIMIT`、`EXTERNAL_QUERY_MAX_CHARS`、
  `EVIDENCE_REVIEW_ADOPTION_LIMIT`、`EVIDENCE_CLAIM_MAX_CHARS`、`EVIDENCE_REVIEW_MISSING_LIMIT`、
  `MISSING_ITEM_MAX_CHARS`。いずれも上流で正規化済みの値を受け取る。
- `QuestionContext.relevant_prior_coverage`は既回答内容(需要側)を表し、検索記録の契約ではない。

### Invariants

- Checkpointの生成・選択にLLMを使わない。決定的な詰め替えと決定的なrenderのみ。
- 過去の記録は検索計画の重複回避・queryの改善・未確認事項の引き継ぎの参考であり、
  過去のclaimがあることだけを理由に検索不要(direct_answer化・task省略)と判断させない。
  現在回答のEvidenceとして直接使用しない。
- Checkpointは同じuser・threadからだけ取得し、raw provider response、URL、source IDを保存しない。
- 記録の存在は検索の実行を意味する。provider呼び出しに成功したqueryだけを記録し、
  失敗したquery・検索を実行できなかったtaskは記録しない。失敗理由も伝達しない
  (非記録により後続Runでは未調査として扱われ、再検索が自然に起きる)。
- `adopted_claims`には外部検索から採用されたclaimだけを入れる。内部記事検索の採用分は保存しない。
- `unresolved_after_search`はEvidence Reviewerの`missing`のverbatim copyであり、
  Run全体で1本としてCheckpointレベルに持つ。
- Question Context Agent、Evidence Reviewer、Answer Agentの責任を変えない
  (Reviewerの出力を読むだけで、Reviewerの仕事は変えない)。
- Checkpointに起因しうる失敗(組み立て・検証・JSON化)は`complete_run`呼び出し前に完結させ、
  失敗時はNULLとして回答を継続する。記録・読出しの失敗で回答Runを失敗させない。

### Non-goals

- source本文・source IDの再利用、embedding検索、thread横断memory、frontend表示。
- 過去Checkpointの上書き、複数Checkpointを統合した永続summary、checkpoint横断の重複除去。
- 過去の検索結果を現在回答の引用根拠にすること。
- 要約生成(Writer)や関連選択(Selector)のLLM化。実測で品質不足が分かった場合に別sliceで扱う。
- 検索失敗(provider障害・期間指定解決失敗等)の失敗理由をPlannerへ伝えること。
  失敗の観測は既存のspan/metricの仕事とする。
- 調査工程直後の先行保存(回答失敗Runの調査記録の保全)。必要と実測されたら別sliceで扱う。

### Done

- 外部検索を実行しcompletedになったRunに、検証済みCheckpointが最大1件保存される。
- 後続Runで同threadの直近3件のCheckpointがPlannerへboundedな独立blockとして渡る。
- Checkpointなし、読出し・検証失敗では既存workflowが同値に継続する。
- 既存の上限を複製せず参照するcontract testが通る。

## 永続化契約

`agent_runs.research_checkpoint`へnullable JSONB columnを追加し、Runと1対1で保持する。
thread、user、message、時刻、statusの検索・所有権は既存relational columnを正本とし、JSONBへ複製しない。

CheckpointはRunning(書き手)とPlanning(読み手)の双方が参照するrun共有の契約であるため、
モデルとrun横断の上限語彙は、工程モジュールへ依存しない既存の共有契約層
`app/agent/contract.py`(Agent core の共有 contract)で定義する。
`app/agent/research_checkpoint/`には工程ロジック(builder・recall)を置く。

```python
class ResearchTaskRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    research_goal: str                  # RESEARCH_GOAL_MAX_CHARS 超過を拒否
    executed_queries: tuple[str, ...]   # provider呼び出しに成功した外部queryのみ。min 1件を型で強制
    adopted_claims: tuple[str, ...]     # 外部検索から採用されたclaim。空 = 有用な候補なし


class ResearchCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    as_of: datetime                            # timezone-aware
    tasks: tuple[ResearchTaskRecord, ...]      # min 1件。0件になるRunはcolumnをNULLにする
    unresolved_after_search: tuple[str, ...]   # Reviewer missingのverbatim copy
```

JSON field名は上記snake_caseを正準形とする。書込み前は`model_dump(mode="json")`、読出し後は
`ResearchCheckpoint.model_validate()`を必須とする。

### 上限

Checkpoint固有のcap定数を新設せず、正本を参照する。builderは切り詰め・再正規化を行わず、
上流で正規化済みの値を詰め替える。Pydantic modelは上限違反を拒否する。

| 対象 | 正本 |
|---|---|
| `tasks`件数 | `RESEARCH_TASK_LIMIT` |
| `research_goal`文字数 | `RESEARCH_GOAL_MAX_CHARS`(本sliceで新設) |
| task内`executed_queries`件数 / 文字数 | `EXTERNAL_TASK_QUERY_LIMIT` / `EXTERNAL_QUERY_MAX_CHARS` |
| Checkpoint全体の`adopted_claims`合計件数 / 1件の文字数 | `EVIDENCE_REVIEW_ADOPTION_LIMIT` / `EVIDENCE_CLAIM_MAX_CHARS` |
| `unresolved_after_search`件数 / 1件の文字数 | `EVIDENCE_REVIEW_MISSING_LIMIT` / `MISSING_ITEM_MAX_CHARS` |

`RESEARCH_GOAL_MAX_CHARS = 200`は本sliceで新設し、既存のdraft→plan正規化で切り詰めを
適用する。上記の上限定数はrun横断の共有語彙として`app/agent/contract.py`で定義し、
planning / external_search / evidence_reviewの各contractはre-exportで既存参照を維持する。
report・Checkpointは正規化済みの値を参照のみ行い、Checkpoint側で独自の切り詰めを行わない。

## 記録フロー

1. Researcherのquery単位のprovider成否を件数への縮約前に保持し、provider呼び出しに成功した
   queryを`executed_queries`としてtask成果物に載せる(agent module内のcontract変更。DBには出ない)。
2. evidence review完了時点で、plan(`research_goal`)+task成果物(`executed_queries`)+
   review結果(task_indexごとの外部採用`claim`、Run全体の`missing`)からCheckpointを
   決定的に組み立て、`model_validate`と`model_dump(mode="json")`まで完了させる。
3. `executed_queries`が空のtaskは記録しない。記録できるtaskが0件ならCheckpointを作らない。
   全taskの候補が0件でevidence reviewを実行しなかったRunは、実行済みtaskを空の
   `adopted_claims`で記録する(`unresolved_after_search`は空)。evidence reviewが
   失敗したRunは採用可否を確定できないためCheckpointを作らない。
4. 組み立て・検証・JSON化の失敗ではCheckpointをNoneとし、安定failure codeのみ記録して
   回答workflowを継続する。
5. `RunResult`へ`research_checkpoint`を追加してrunnerの外へ運び、`complete_run`が
   completedへのUPDATEと同一トランザクションで最大1回保存する。トランザクション失敗は
   回答の永続化失敗であり、既存のRun失敗処理に従う(Checkpoint固有のretry・別経路保存は設けない)。
6. 外部検索を実行しなかったRun(direct_answer含む)、失敗・中断したRunはNULLのままとする。

## 注入フロー

1. 既存どおり`QuestionContext`を1回準備する。
2. 新規repository queryで、同thread・同user・`status='completed'`・
   `research_checkpoint IS NOT NULL`のRunを新しい順に最大3件読む(ownership filterはthread joinで強制)。
3. 各JSONBを`model_validate`し、失敗したCheckpointは候補から除外する(件数とfailure codeのみ記録)。
4. 検証済みCheckpointは`PlanningRequest.prior_research`(`tuple[ResearchCheckpoint, ...]`)として
   Plannerへ渡し、planner prompts側で新しい順に決定的にrenderして`planning-only`かつ
   `untrusted`な独立blockとして注入する。再要約・重複除去は行わない。
   Checkpointが0件なら現行と同値のPlanner入力とする。

## Planner prompt contract

Planner promptをv7へbumpする。出力schemaは変更しない。以下は確定文言であり、
実装はこのcode blockと一字一句一致させる。

### instructionsへ追加するsection

「# searchの計画」の直後、「# target_time_window」の前に追加する。

```text
# Prior Research Contextの使い方
入力のPrior Research Contextは、同じthreadの過去の調査記録である。
検索計画の参考にのみ使い、現在回答の事実根拠として使わない。

- 得られたことを前提に、まだ得られていない情報や、質問が求めるより広い・深い情報へ
  調査を向ける。
- 実行済みqueryと同じ・同義のqueryは、鮮度の再確認が目的の場合を除き繰り返さない。
  過去のqueryを踏まえて角度・具体性を改善する。
- 過去に情報が得られていることだけを理由に、検索を省略したりdirect_answerを
  選んだりしない。現在の質問に必要な検索は改めて計画する。
```

未確認事項の扱いは規則にしない(関連すれば使う・無関係なら無視するはdefaultの振る舞いで、
規則が行動を変えないため)。renderされたデータからのLLMの判断に委ね、問題が実測されたら
プロンプトを調整する。

### 入力テンプレートへ追加するsection

「# Conversation Context」の後、Repair Contextの前に追加する。Checkpoint 0件では
sectionごと出力しない。

```text
# Prior Research Context
同じthreadの過去の調査記録(新しい順)。

<untrusted_prior_research>
{records}
</untrusted_prior_research>
```

### recordsのrender規則

checkpointごとに新しい順へ、次の形へ決定的にrenderする。

```text
[調査時点: {as_of}]
research_goal: {research_goal}
実行したquery:
- {query}
得られたこと:
- {claim}
未確認のまま残ったこと:
- {unresolved}
```

- taskが複数あるcheckpointは「research_goal / 実行したquery / 得られたこと」の組を
  taskごとに繰り返し、「未確認のまま残ったこと」はcheckpointレベルで最後に1回出す。
- `adopted_claims`が空のtaskは「得られたこと:」に`- 有用な候補は得られなかった`を出す。
- `unresolved_after_search`が空なら「未確認のまま残ったこと」を節ごと省略する。
- render対象の全文字列は`sanitize_for_untrusted_block()`を通す。as_ofはシステム生成の
  timestampをisoformatで出す。

## Failure・安全性

- model-visibleへrenderする文字列(research_goal、query、claim、unresolved)は
  `sanitize_for_untrusted_block()`を通す。
- log、metric、traceには件数、schema version、安定failure code、run_id等の識別子だけを
  記録し、query、research_goal、claim本文を載せない。
- 記録・読出し・検証のどの失敗も回答Runの停止条件にしない。

## Test contract

- JSONB round-trip、`extra="forbid"`、timezone-aware `as_of`、既存capの参照(複製の不在)。
- `research_goal`がdraft→plan正規化で`RESEARCH_GOAL_MAX_CHARS`へ切り詰められる。
- provider失敗queryが`executed_queries`に入らない。検索未実行taskが記録されない。
  記録可能task 0件・検索なしRun・direct_answer RunでcolumnがNULL。
- `adopted_claims`に外部採用claimのみが入り、内部採用claimが入らない。
- `unresolved_after_search`がReviewer missingと一致する(verbatim copy)。
- completed以外のRunにCheckpointが書かれない。builder失敗でも回答Runが継続する。
- 読出しqueryがuser/thread ownership・completed・NOT NULLでfilterされる。invalid JSONBはskipされる。
- rendererが直近3件だけをuntrusted blockへ新しい順に出し、0件で現行同値のPlanner入力になる。
