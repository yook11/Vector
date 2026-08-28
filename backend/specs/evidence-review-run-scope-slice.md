# Evidence Review を run 単位へ広げる slice 仕様

更新日: 2026-07-30

実装状況: Draft

## 位置付け

本sliceは、Evidence Reviewer の精査単位を`ResearchTask`単位からRun単位へ広げる。

`research-task-evidence-selection-slice.md`の段4で、Evidence Reviewer は1つの`ResearchTask`について
内部候補と外部候補を同じ基準で精査する役割として導入された。その結果、内部記事にも`claim`が付き、
出所による非対称は解消された。残っているのは判断の視野である。reviewer はいま自分のtaskの候補しか
見ておらず、「集めた全部を踏まえて何が満たせていないか」を言える主体がRunの中に存在しない。

責務の言い方は変えない。「読んで精査し、採用できるものと足りないものを見極める」ままである。
変えるのは、その見極めが依拠する視野をtaskからRunへ広げることである。

前提: `research-task-evidence-selection-slice.md`の段1〜段5は実装済み(PR #71〜#75、#78でmainへ到達)。

## Work Definition

### Problem

- 「何ができていないか」の判断がtaskの担当範囲に閉じている。reviewer は自分のtaskの`research_goal`と
  候補だけを見て`missing`を書くため、他のtaskが何を取れたかを知らない。あるtaskで取れなかった論点が
  別のtaskの候補で埋まっていても、埋まったことを誰も観測できない。
- 逆に、どのtaskも部分的にしか取れなかった論点は、どのtaskからも`missing`として挙がらないことがある。
  各taskは自分の担当範囲では説明できたと判断しうるためである。
- 最終回答の`missing_aspects`はtask単位の`missing`を連結して重複を除いたものであり
  (`result_assembly._external_task_missing()`)、Run全体としての不足を表す主体がいない。
  並んでいるだけで統合されていない。
- `content_requirements`(この回答に含めるべき内容)はRun単位の宣言だが、その充足判断はtask単位で
  分散している。ユーザーが求めた内容が全体として満たせたかを判断する場所がない。

### Evidence

- `EvidenceReviewer.review()`は全taskの候補をまとめた`EvidenceReviewPreparation`を受け取り、
  `EvidenceRunResult`(`EvidenceRunCompleted`または`EvidenceRunFailed`)を返す。成功は
  `answer_evidence` / `review_missing`、失敗は`failure_code`を持つ。
- `AnsweringRunner._fan_out_tasks()`はtask並列でcollect+reviewを回し、全taskの結果を合流させてから
  内部根拠を`curation_id`、外部根拠をURLで先勝ち重複排除する。重複排除は精査より後にある。
- `build_review_candidate_projection()`は内部候補を先(index 0..n-1)、外部候補を後(n..)に置いた
  単一index空間を作る。渡すfieldは`index` / `title` / `source_name` / `published_at` /
  `snippet`(500字)のみで、URL、`assessment_id`、`curation_id`、出所種別を含めない。
- reviewer の出力は`selections[]`(`candidate_index` / `claim` / `why_selected`)と`missing[]`である。
  出典メタデータは返させず、`AnswerEvidence.from_reviewer_response()`が`candidate_index`で
  候補列を引いて再構築する。
  範囲外indexと重複indexは決定的に不採用となる。
- reviewer response のselection上限と回答へ渡す根拠の上限は`ANSWER_EVIDENCE_LIMIT`(15)である。
  回答が使える件数が上限の根拠であり、reviewer response schemaはそれに従う。
- 確定根拠は見せた候補の`option_index`を持つ。citation用の`source_ref`文字列は
  `build_answer_input_evidence()`が連番で振る。
- `EVIDENCE_REVIEWER_AGENT`は`deepseek-v4-flash`、`max_output_tokens=2048`。timeout 30秒、
  最大2 attempt。2回とも分類済み失敗ならRunは根拠ゼロで終わり、`EvidenceRunFailed`へ
  `failure_code`が残る。
  候補が内外ともゼロのtaskはLLMを呼ばず`review="skipped_empty"`となる。
- 1 Runの候補数上限は、task 3件 × (内部5件 + 外部20件) = 75件。内部は`InternalSearchTool`が
  task内で`curation_id`集約するためtask内重複はないが、task間では同じ記事が現れうる。
  外部の候補poolも`EXTERNAL_SEARCH_CANDIDATE_POOL_LIMIT_PER_TASK`(20)がtask単位である。
- 採用の実質上限は task 3件 × 5件 = 15件。`claim`と`why_selected`が各300字のため、
  15件を返すには出力9,000字に`missing`分が加わる。現行の`max_output_tokens=2048`はtask単位
  5件(3,000字 + missing 1,000字)を前提とした値である。
- 進捗event`ExternalSearchEvidenceSelectedEvent`は`task_index`と`evidence_count`を持ち、
  runner がtaskごとに1回発火する。`evidence_count`は内部採用数と外部採用数の合算である。
  frontend の parser(`live/events.ts`)は`taskIndex`を非負整数として必須検証し、
  欠けた場合はそのeventを`null`として捨てる(fail-soft)。
- `phase="evidence_review"`のAgent phase spanは`agent_name`と`task_index`属性を持つ。

### Invariants

#### Run 単位で精査する

- Evidence Reviewer Agent の呼び出しはRunにつき1回とする。全taskの収集が完了してから起動する。
- reviewer は全taskの候補を1回の入力で受け取り、Run全体としての採用と不足を1つの出力で返す。
  task単位の精査呼び出しは廃止する。
- 収集(Researcher)はtask単位の並列を維持する。並列の軸を変えない。変わるのは収集完了後に
  reviewer が1回走ることである。
- 精査がRunで1回になることで、収集の完了を待つ合流点が精査の前に生まれる。最も遅いtaskの収集完了が
  精査開始の条件になる。

#### 候補の渡し方

- 候補は`research_goal`ごとにグループ化して渡す。どの調査目的のために集めた候補かが入力の構造から
  読めるようにする。goalと候補を平坦に並べて対応を推測させない。
- index はグループをまたいだRun全体の通し番号とする。グループごとに0から振り直さない。
  グループと番号の組で候補を指させると、組の取り違えが復元時の誤りになる。番号だけで一意に
  決まる形を保つ。
- グループ内の並びは内部候補を先、外部候補を後とする。既存の`build_review_candidate_projection()`の
  順序をグループ単位で維持する。
- 候補projectionのfield構成、URL・id・出所種別を渡さない制約、`sanitize_for_untrusted_block()`と
  `<untrusted_input>`境界は変更しない。
- 精査前の候補にtask間の重複排除を行わない。同じ内部記事が複数グループに現れることは、目的ごとの
  収集として自然である。reviewer は同じ記事を別々の観点で採用してよい。
- `content_requirements`はRun単位の宣言としてグループの外に置く。goalごとに複写しない。
- `standalone_question`は渡さない。`research_goal`と`content_requirements`で「求められていること」は
  足りており、質問文そのものを共有しない既存の判断を維持する。

#### 選別結果の復元

- reviewer には`candidate_index`だけを返させる。所属グループや`task_index`を返させない。
  index からグループは一意に決まり、モデルに冗長な情報を返させると不整合の余地が増える。
- index から候補と所属taskを引く逆引きは`EvidenceReviewPreparation`が持ち、
  `AnswerEvidence.from_reviewer_response()`が範囲外indexと重複indexを決定的に不採用とする。
  不採用件数はproduction contractへ含めない。
- 見せる候補は同一task内で同じ内部記事・同じURLを重ねない。先に並んだhitだけを見せる。
  同じ記事でもtaskが違えば別の枠として見せ、選ばれれば別の根拠として残す。
- `AnswerEvidence`は回答に使用できる確定済み根拠の集合であり、内部根拠と外部根拠を一つの契約で
  保持する。task内の出典identityとoption_indexの一意は型の不変条件とする。回答上限はその集合の
  件数へ適用する。
- `missing`上限をRun単位8件とする。task単位5件×3 taskの実質上限(15件)より絞る。Run全体の不足の
  表明として、同じ論点の言い換えが並ぶことを避ける。
- reviewer response のselection上限と回答に使用する根拠上限は同じ制約であり、`ANSWER_EVIDENCE_LIMIT`を
  正本として共有する。上限の根拠は回答が使える件数にあり、工程上の出力制約はそれに従う。
- `claim`と`why_selected`のcap(各300字)、`missing`項目のcap(200字)は変更しない。
- `max_output_tokens`を16,384へ引き上げる。上限まで採用したときの出力量は
  selections 15件×(300字+300字+JSON構文) + missing 8件×200字 で概算11,400字であり、
  現行の2048では確実に足りない。DeepSeekは文字→token比を英語0.3 / 中国語0.6と公表し日本語は
  記載がないため、保守側に1.0 token/字を仮定して11,400 tokenを見積り、約1.4倍の余裕を取る。
  `deepseek-v4-flash`の最大出力は384K tokenであり、この値はmodel上限と競合しない。
- 採用された根拠の`task_index`を維持する。`task_index`はresearch taskの識別子であり、
  精査がRun単位になっても値の意味は変わらない。
- 確定根拠は見せた候補の`option_index`を保持する。citation用の`source_ref`文字列は
  `build_answer_input_evidence()`が連番で振り、ユーザー向けの引用番号になる。

#### 採用の言語化

- `claim`と`why_selected`の役割をinstructionsで定義する。現行は「日本語で書く」しか指示がなく、
  2欄の違いを担っているのはfield名だけである。どちらも「根拠」と読めるため役割が重なる。
- `claim`は、その候補が報じている主張を1文で書く。主語は候補であり、reviewer が`research_goal`に
  対して立てる主張ではない。この一文だけを読んで何の記事かがわかる文にする。
- `claim`に候補へ書かれていないことを書かせない。reviewer は記事本文を読まず`title`と
  `snippet`(500字)しか見ていないため、推測を許すと外部出典として表示される文へ幻覚が入る。
  現行instructionsはメタデータの捏造だけを禁じ、内容の捏造を禁じていない。
- `claim`に`research_goal`や選定の理由を書かせない。この2つは`why_selected`の担当である。
- `why_selected`は、その候補を`research_goal`に対して選んだ根拠を書く。
- `claim`の露出の非対称を変更しない。外部根拠は`ExternalUrlSource.evidence_claim`としてAPI・UI・DBへ
  出る(DB CHECKで非空必須)。内部根拠は`InternalArticleSource`に該当fieldがなく、回答Agentへ渡す
  本文の先頭行にのみ入る。
- `why_selected`は現状どこでも消費されていない。本slice で消費先を追加しない。
- instructionsの変更に伴い`EVIDENCE_REVIEWER_PROMPT_VERSION`をv1からv2へ上げる。

#### 何ができていないかの表明

- `missing`はRun全体の不足として1本にする。task単位の`missing`を連結して重複を除く経路
  (`result_assembly._external_task_missing()`)を廃止する。
- reviewer は`research_goal`の集合と`content_requirements`に照らして、全候補を読んだ上で
  何が満たせていないかを書く。あるgoalの不足が別のgoalの候補で埋まっている場合は挙げない。
- 収集そのものが完了しなかったtaskがある場合の固定文言(「完了できなかった調査があります」)は
  維持する。これは収集の失敗の表明であり、精査による不足判断とは別の事実である。
- reviewer が2 attemptを使い切って失敗した場合、そのRunは根拠ゼロで終わる。距離順やprovider
  rank順で採用するfallbackを設けない。精査を通っていない候補を出典として提示しない。
- 精査失敗専用の文言を追加しない。根拠ゼロは既存の`_RETRIEVAL_EMPTY_MISSING`
  (「回答に使える根拠を取得できませんでした」)が`include_retrieval_empty_missing`経由で表明し、
  `status`は`insufficient`になる。文言が収集の失敗にも読める点は受け入れる。どの工程で落ちたかは
  運用者の関心であり、`failure_code`とspan属性`review_failure_code`で観測する。
- `status`(`answered` / `insufficient`)は`missing_aspects`から導出される既存規則をそのまま使う。
  個別に設定しない。

#### 合流と重複排除

- `AnswerEvidence.from_reviewer_response()`は見せた番号の選択だけを復元する。範囲外indexと
  同じindexへの矛盾したclaimはその番号を不採用とする。出典identityの畳み込みは行わない。
- 不採用件数はreportへ残さない。
- 合流後の`AnswerInputEvidence`への正規化、最終`source_ref`の通し番号採番、回答Agentの入力契約、
  `cited_refs`の検証は変更しない。

#### 観測と失敗分類

- reportを収集と精査で分ける。収集系(内部/外部の収集status、生成query、provider失敗数、
  候補件数)はtask単位のまま残す。精査系(精査status、確定根拠件数、`missing`)はRun単位へ移す。
- `task_index`はresearch taskの識別子であり、精査の帰属を表す値ではない。精査がRun単位になっても
  意味と用途を変えない。採用根拠、収集側のspan、進捗eventはこれまでと同じように`task_index`を持つ。
- task別の採用内訳は index の逆引きから算出する。reviewer に返させない。
- 進捗event`ExternalSearchEvidenceSelectedEvent`のSSE契約を変更しない。精査成功後に採用根拠を
  `task_index`でグループ化し、候補があったtaskについて`task_index`昇順で1回ずつ発火する。
  `evidence_count`は当該task由来の採用件数(内部+外部)とする。精査が失敗したRunでは発火しない
  (現行のtask単位失敗時と同じ)。frontend の parser と表示は変更を必要としない。
- `phase="evidence_review"`のAgent phase spanはRunにつき1回になる。このspanは全taskを覆うため
  `task_index`属性を持たない。収集側のspan(`external_query`)は`task_index`属性を維持する。
- `EvidenceReviewRecorder.record()`がRun 1回のspan・duration・最終outcomeを完結させる。
  成功と分類済み縮退は`completed`、未分類例外は`failed`、cancelは`stopped`としてdurationへ残す。
- `vector.agent.evidence_review.outcome`は`result`(`succeeded | failed`)、`attempt_count`、
  `failure_code`を持つ。成功時の`failure_code`は`none`とし、retry途中の失敗は記録しない。
- span属性、event、status descriptionに質問本文、履歴、prompt、query text、candidate snippet、
  evidence本文、回答本文を載せない既存制約を維持する。
- `AnswerProgressStage`(`planning` / `retrieving` / `synthesizing`)の語彙と発火順序を変更しない。

### Non-goals

- 進捗stageの語彙を`planning` / `evidence_collection` / `evidence_review` / `answering`へ
  改めること。DB CHECK制約、SSE、frontend表示を含む別sliceとして扱う。
- `external_search.*` event名の出所非依存化。
- 収集の並列度policy、候補pool上限、内部検索の件数上限を変更すること。
- 不足を検出したときに再調査へ回すこと。`missing`はRunの表明に留め、収集をやり直さない。
- 回答Agent(direct / evidence)のprompt、契約、streaming機構を変更すること。
- planner、Question Context Agent、Input Safety Agent の宣言を変更すること。
- DB schema、API response shape、新規dependency。

### Done

- Evidence Reviewer Agent の呼び出しがRunにつき1回であり、全taskの候補を1回の入力で受け取る。
- 候補入力が`research_goal`ごとにグループ化され、index がグループをまたいだ通し番号である。
- `missing`がRun全体の不足として1本で表明され、task単位の連結経路が消えている。
- `AnswerEvidence`が回答に使用する確定済み根拠を表し、内部`curation_id`と外部URLの重複排除が
  精査後の復元時に効いている。
- 採用上限15件と`missing`上限8件がRun単位の定数として定義され、`max_output_tokens`が上限まで
  採用しても切れない値になっている。
- 精査が失敗したRunが根拠ゼロで`insufficient`になり、未精査候補を出典として提示しない。
- 進捗eventのSSE契約が変わらず、frontend の変更を伴わない。
- 既存のregression(回答shape、citation検証、progress stage、resource lifecycle、text非露出)が
  すべて通る。

## 責任境界

| 責任 | AnsweringRunner | Researcher | Reviewer | Agent | Composition |
|---|:---:|:---:|:---:|:---:|:---:|
| task並列度policy | ○ | - | - | - | - |
| task内の収集順序 | - | ○ | - | - | - |
| 外部query生成 | - | 起動 | - | 実行 | 配線 |
| 外部HTTP検索 / 内部vector検索 | - | 起動 | - | - | 配線 |
| 候補pool構築 | - | ○ | - | - | - |
| 収集完了の待ち合わせ | ○ | - | - | - | - |
| グループ化した候補列の構築 | - | - | ○ | - | - |
| 根拠の選別と不足の見極め | - | - | 起動 | 実行 | 配線 |
| index→出典・task の再構築 | - | - | ○ | - | - |
| 出典復元・重複排除・回答上限 | - | - | ○ | - | - |
| citation検証とfinal assembly | ○ | - | - | - | - |

## 目標実行順

```text
AnsweringRunner.run
├─ Input Safety Agent
├─ Question Context Agent
├─ hook
├─ Question Planner Agent          → DirectAnswerPlan | SearchPlan
├─ DirectAnswerPlan → Direct Answer Agent
└─ SearchPlan
   ├─ target_time_window 解決
   ├─ external runtime scope activate
   ├─ [ResearchTask ごとに並列、最大3]
   │  └─ Researcher.collect(task)   → 内部候補 + 外部候補pool
   ├─ 全taskの収集完了を待つ
   ├─ Evidence Reviewer Agent(1回)  → 採用 + claim + Run単位のmissing
   │                                   入力は goal ごとにグループ化した候補列
   ├─ 内部根拠の重複排除
   └─ Evidence Answer Agent
```

## 実装順(提案)

1 PRで通す。変更の実体は「精査の視野をtaskからRunへ広げる」1つであり、段に割ると中間状態で
reviewer の呼び出し単位とcapの単位が食い違う。回答Agentへ渡る根拠の総量(最大15件)を変えないため、
段4のように採用規則の差分を切り離して観測する必要もない。

## 確認した一次情報

- `deepseek-v4-flash`はcontext 1M / 最大出力384K token([Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/))。
- 文字→token比の公表値は英語0.3 / 中国語0.6で、日本語の記載はない([Token & Token Usage](https://api-docs.deepseek.com/quick_start/token_usage/))。
  日本語は公表値がないため、`max_output_tokens`の見積では1.0 token/字を保守側の仮定として置く。

## Test contract

### 精査の呼び出し単位

- 3 taskのSearchPlanで reviewer の呼び出しが1回だけ起きる。
- reviewer の入力に全taskの`research_goal`とその候補がグループとして含まれ、index がグループを
  またいで重複しない。
- 全taskの収集が完了する前に reviewer が起動しない。
- いずれかのtaskの収集が失敗しても、残った候補で精査が走る。
- 全taskの候補が内外ともゼロのとき reviewer を呼ばない。

### 選別結果の復元

- グループをまたいだ index から、候補と所属taskが正しく引かれる。
- 範囲外indexと重複indexが決定的に不採用となる。
- 同じ内部検索の記事が同一task内に複数あっても、見せる枠は先の1件だけである。
- 同じ内部検索の記事でもtaskが異なる場合は、両方見せ、選ばれれば両方の根拠として残る。
- 同じURLが同一task内に複数あっても、見せる枠は先の1件だけである。
- 同じURLでもtaskが異なる場合は、両方見せ、選ばれれば両方の根拠として残る。
- 確定根拠に`ANSWER_EVIDENCE_LIMIT`が適用される。

### 不足の表明

- reviewer の`missing`がRun単位で1本として`missing_aspects`へ流れる。
- `missing`が8件を超えて返っても8件へclampされる。
- 収集が完了しなかったtaskがある場合、固定文言が`missing_aspects`へ1行加わる。
- `missing_aspects`が空でなければ`status`が`insufficient`になる。

### 精査の失敗

- reviewer が2 attempt失敗したRunが根拠ゼロで終わり、未精査候補が根拠に混入しない。
- 精査失敗時に`missing_aspects`へ「回答に使える根拠を取得できませんでした」が入り、
  `status`が`insufficient`になる。
- 精査失敗時に`ExternalSearchEvidenceSelectedEvent`が発火しない。

### 進捗event

- 精査成功後、候補があったtaskについて`task_index`昇順で1回ずつ発火する。
- `evidence_count`が当該task由来の採用件数(内部+外部)と一致する。
- 候補が内外ともゼロだったtaskについては発火しない。

### 採用の言語化

- `EVIDENCE_REVIEWER_AGENT.prompt.version`が`"v2"`である(現行テストが`"v1"`を2箇所で検証している
  ため追随が必要)。
- instructionsに`claim`と`why_selected`の定義が含まれ、prompt resourceの外で組み立てられていない。
- prompt injection sentinelがinstructionsへ混入しない既存の境界テストが通る。

### 非露出

- span属性とeventに候補snippet、evidence本文、query text、質問本文が載らない。

## 影響範囲

- `app/agent/evidence_collection/evidence_review/` — contract(入力型のグループ化、cap の
  Run単位化)、prompts(グループ構造のレンダリング、`claim`と`why_selected`の定義追記、
  PROMPT_VERSION v2)、agent(`max_output_tokens`の引き上げ)、policy(projection構築とindex逆引き)、
  reviewer(呼び出し単位とphase spanの属性)
- `app/agent/evidence_collection/external_search/policy.py` — URL重複排除の削除
- `app/agent/running/answering_runner.py` — collect と review の分離、収集完了の待ち合わせ、
  report の収集/精査分割
- `app/agent/answering/result_assembly.py` — `missing_aspects`の組み立てをRun単位の`missing`へ
- `app/agent/composition.py` — reviewer の配線(呼び出し単位の変更に伴う引数)

`app/agent/contract.py`と`app/schemas/research.py`は変更しない。進捗eventの型と発火fieldを維持し、
発火する場所と件数の算出だけが変わる。

frontend は変更しない。SSE契約とevent名を維持するため、parser と表示文言の追随を必要としない。

DB schema変更なし。API response shape変更なし。新規dependencyなし。

### 実装後に確認する運用値

- reviewer の入力量が task単位最大25件からRun単位最大75件へ増える。`snippet`が500字のため
  入力は概算で3倍になる。latency と入力トークン量を実測する。
- task が1件のRunでは採用上限が5件から15件へ増える。3 taskのRunでは実質上限が変わらないが、
  planner が1 taskで済ませたRunでは回答Agentへ渡る根拠が増えうる。plan の task数分布と
  採用件数の関係を実測する。
- 精査が収集完了後の1回になるため、収集と精査のpipeline重なりが無くなる。Run全体の実時間が
  どう変わるかを実測する。
- 採用件数の分布と、内部根拠が引用されない回答の頻度。視野がRunへ広がることで出所の偏りが
  変わる可能性がある。既存の採用数・引用数span属性を分母に使う。
