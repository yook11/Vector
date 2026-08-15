# Research task 単位の根拠収集と選別 slice 仕様

更新日: 2026-07-28

実装状況: Draft

## 位置付け

本sliceは、回答Runの根拠取得を「調査目的(research task)」単位へ再編し、内部検索結果と外部検索結果を
同じ判断基準で選別する役割を新設する。

`agent-declaration-runner-orchestration-slice.md`のPR10で扱う予定だった「`AnsweringRunner`から
external pipelineを切り出す」作業を含むが、単なる移設ではない。責務の分割線そのものを引き直す。

責務を3つに分ける。

- 集める: 1つの調査目的に対し、内部記事と外部ニュースの候補を集める
- 精査する: その調査目的に照らして候補を読み、採用できる根拠と足りないものを見極める
- 答える: 精査を通った根拠で回答する

前提: PR0〜PR9(Agent宣言、`AgentRuntime`、External Search Tool、`ExternalResearchRuntimeFactory`、
workflow ownership、retrieval dispatch、external pipeline統合)は実装済み。

## Work Definition

### Problem

- 内部検索の結果は「回答目的に照らして根拠になるか」を一度も判断されないまま回答Agentへ渡る。
  選別基準はベクトル距離だけであり、意味が近いことと根拠になることが同一視されている。
- 内部と外部で根拠の質が非対称である。外部根拠は`claim`(何を裏付けるか)を持つが、内部根拠は
  `summary`と`key_points`をそのまま連結した本文であり、調査目的との関係が言語化されていない。
- 内部検索には調査目的が存在しない。`SearchPlan.article_search_queries`は検索文字列の列であり、
  `external_research_tasks`の`research_goal`と対応づけられていない。並んでいるだけで紐づいていない。
- `AnsweringRunner`(782行)の48%がexternal pipeline専用のprivate methodで占められ、
  `run()`から回答workflowの高レベル順序を追えない。
- 選別の失敗単位と収集の失敗単位が食い違っている。外部はtask単位で部分成功できるが、内部は
  run単位の`collection_failures`でしか失敗を表現できない。

### Evidence

- `InternalSearchService.search_articles()`は`per_query_limit=5` / `limit=5`固定で、
  query embeddingごとにvector検索し、`curation_id`で最小distanceのhitを残し、distance昇順で
  上位5件を返す。LLMによる判断は一切入らない。
- `InternalSearchQueries`は構築時に空白のみのqueryと`MAX_ARTICLE_SEARCH_QUERIES`(3)超過を
  拒否する。重複排除は行わない。
- 内部の進捗event(`InternalSearchStartedEvent` / `InternalSearchCompletedEvent`)は
  `InternalSearchService`自身が発火する(開始時にstarted、成功時のみcompleted、失敗時は
  completedなし)。外部の進捗eventはrunnerが発火する。serviceは併せてresult / failure_phaseの
  metricsを記録する。
- `build_answer_input_evidence()`は内部hitを先に、外部evidenceを後に並べ、`source_ref`を
  `"1"`から通し番号で振り直す。内部本文は`summary`と`key_points`の連結、外部本文は
  `claim`と`snippet`の連結である。
- `EXTERNAL_EVIDENCE_SELECTOR_AGENT`の入力は`research_goal`、`as_of`、URLを含まない候補
  projection(`index` / `title` / `source_name` / `published_at` / `snippet`)であり、
  出力は`selections[]`(`candidate_index` / `claim` / `why_selected`)と`missing[]`である。
  最大5件、`claim`と`why_selected`は各300字、`missing`は5件×200字でclampされる。
- selectorの`missing`は`ResearchTaskReport.missing`を経由し、`result_assembly.py`の
  `_external_task_missing()`で最終回答の`missing_aspects`へ流れる。selectorの出力は
  ユーザーに直接見える。
- 出典メタデータ(URL / title / snippet / published_at / source_name)はselectorの出力に含まれず、
  `build_external_evidence()`が`candidate_index`でpoolを引いて再構築する。範囲外index、重複index、
  範囲外indexと重複indexは決定的に不採用となる。
- runnerは全taskのevidenceを連結した後、`deduplicate_external_evidence_by_url()`でURL完全一致の
  先勝ち重複排除を行う。`ResearchTaskReport.evidence_count`はdedup前の値であり、dedup後件数と
  dedup件数の和との整合をvalidatorが検証する。内部hitのrun内重複は`InternalSearchService`の
  `curation_id`集約が構造的に防いでいる。
- `SearchPlan`は`article_search_queries`(1〜3件)と`external_research_tasks`(1〜3件)を持つ。
  両者に対応関係を与えるvalidatorも構造もない。`QuestionPlanDraft`側も
  `article_search_queries: list[str]`と`research_goals: list[str]`の2本の平坦なlistである。
- `AnsweringRunner._collect_evidence()`は`_gather_search_branches()`で内部枝と外部枝を並列に走らせ、
  外部枝の内側で`_execute_external_pipeline()`がtask単位のfan-out(semaphore、既定並列度は
  task数とhard limit 3の小さい方)を行う。すなわち並列の軸が2段になっている。
- external branchは`ExternalResearchRuntimeFactory.activate()`のscope内でだけDeepSeek clientと
  Tavily clientを開く。direct pathとinternal-only pathでは開かない。
- `ResearchTaskStatus`は`succeeded` / `query_generation_failed` / `provider_failed` /
  `selector_failed` / `time_filter_failed`の5値。`time_filter_failed`はplan単位の日付解決失敗を
  全taskへ複写したものであり、この場合そのtaskは根拠ゼロになる。
- `AnswerProgressEvent`は`InternalSearchStartedEvent(query_count)` /
  `InternalSearchCompletedEvent(hit_count)`(いずれも`task_index`なし)と、
  `ExternalSearch*Event`(いずれも`task_index`あり)で構成される。これらはSSE経由でfrontendが消費する。
- `collection_failures`が設定されるのは、内部枝が`InternalSearchError`を捕捉したときの
  `["internal_search"]`のみである。`"external_search"`はLiteralに定義されているが設定する
  経路がない。
- planner instructions(`PLANNER_PROMPT_VERSION = "v4"`)は`article_search_queries`と`research_goals`を
  別々の節で説明し、両者の関係を規定していない。

### Invariants

#### Research task を単位とする

- `SearchPlan`は1〜3件の`ResearchTask`を持つ。`ResearchTask`は`research_goal`(調査目的、日本語)と
  `article_search_queries`(内部ベクトル検索用の検索文)を関連づける。
- 収集と選別はどちらも`ResearchTask`単位で完結する。1つのtaskの失敗が他のtaskの根拠を消さない。
- task間の並列度policyは既存の`resolve_external_search_agent_count()`(task数、要求値、hard limit 3の
  最小値)を維持する。内部検索が加わることで並列度の意味は変わらない。
- 並列の軸をtask単位の1段に統一する。「内部枝 ∥ 外部枝」というrun単位の並列は廃止する。
  1つのtaskの内部収集と外部収集は、そのtaskの中で並列に走る。

#### 集める(Researcher)

- Researcherは1つの`ResearchTask`に対して内部候補と外部候補を集める責任だけを持つ。
  選別、根拠の言語化、回答生成を所有しない。
- 外部収集は現行どおり External Search Query Agent が`research_goal`から英語keyword queryを
  1〜3件生成し、External Search Tool がquery単位で並列に検索し、`build_candidate_pool()`が
  provider rankをinterleaveしてURL重複を排除した候補pool(最大20件)を作る。
- 内部収集は`ResearchTask.article_search_queries`を Internal Search Tool へ渡し、
  現行の`InternalSearchService`と同じ検索(query embedding、vector検索、`curation_id`重複排除、
  distance昇順)で候補を得る。task単位の取得件数上限は現行値5を維持する。
- `MAX_ARTICLE_SEARCH_QUERIES`(3)は run 全体の合計上限とする。1回のRunが内部検索に使うqueryの
  予算であり、plannerがtaskへ配分する。task単位の上限ではない(3 task × 3 queryを許さない)。
  各taskは最低1件を持つ。`SearchPlan`のvalidatorが合計超過を構造的に拒否する。
  上限はtailを縛るためのものであり、実際の件数はplannerの判断に委ねる。上限を下げて典型的な
  件数を制御しない。
- response schemaはtask横断の合計を表現できないため、予算の強制はdraft finalizeが行う。
  合計超過のdraftはdefectにせず、各taskの先頭queryからtask順のround-robinで予算まで決定的に
  trimする。既存の`_clean_plan_queries`が超過を黙って切り詰めるのと同じ扱いであり、schemaで
  防げない超過をdefectにすると回復可能な条件でplanner attemptを浪費する。trimはどのtaskも
  0件にしない(task数≤3、予算3のため各taskの先頭1件は必ず残る)。
- queryのcasefold重複排除はtask内のみとする。task間の重複queryは許容する。排除すると後続task
  のqueryが全滅した場合の扱いという不要な分岐を生み、`research_goal`が一意なら実際には稀で、
  予算が総量を縛る。現行のrun単位一意性からの緩和である。
- planner instructionsで内部検索クエリの水増しを禁じ、予算3件をtaskへ配分する指示を与える。
  同じ角度の言い換えを並べない、角度が1つなら1件でよい、という規律を
  External Search Query Agentの既存instructionsと揃える。
- 1 task内のvector検索はquery単位で順次に実行し、並列化しない。合計予算が3のため並列化しても
  同時実行は最大3で変わらず、構造だけが複雑になる。DB同時接続ピークの上限はtask並列度(最大3)
  である。query embeddingは現行どおりtask内の全queryを1回のAPI呼び出しへbatchする。
- 内部収集と外部収集はbest-effortとする。片方が失敗しても、候補が1件でも残っていれば選別へ進む。
  両方が候補ゼロで終わったtaskだけを収集失敗とする。
- `target_time_window`の解決失敗は外部収集だけを失敗させる。現行のように task 全体を根拠ゼロに
  しない。内部候補が得られていれば選別へ進む。
- 収集の進捗event(内部検索の開始・完了、外部queryの生成、候補取得)はResearcherが発火する。
  ToolとAgentは進捗eventを発火しない。選別の進捗eventは選別を起動する側(runner)が発火する。

#### Tool 契約の統一

- Internal Search Tool を External Search Tool と同じ形の契約にする。stable name、typed input /
  output、`invoke` port、分類済みfailure contractを持ち、workflow分岐とmodel判断を所有しない。
- 契約を揃える理由は、同じconsumer(Researcher)が両方を隣り合わせに呼ぶためである。呼び出し側が
  2つの異なる形を覚えなくてよくなる場合にだけ揃える、という親仕様の条件をここで満たす。
- ToolはSSE用の進捗eventを発火しない。現行は`InternalSearchService`自身が発火しており、
  Tool契約化と同時に発火を呼び出し側へ移す。発火地点は現行の鏡写し(開始時にstarted、
  成功時のみcompleted、失敗時はcompletedなし)を保ち、SSE契約を変えない。
- result / failure_phaseのmetricsはTool実装に残す。検索操作の決定境界を所有する者が記録する。
- Internal Search Tool の失敗は既存の`InternalSearchFailurePhase`
  (`query_embedding` / `article_search`)を安全なreasonとして公開する。SQLAlchemy例外、
  接続文字列、query本文をreasonへ載せない。分類外の例外をこの語彙へ丸めない。
- embedding生成はInternal Search Toolの実装詳細として扱い、独立したToolやAgentにしない。
  embedding cacheの配線状態(現在composition未注入)を本sliceで変更しない。
- Toolはmodelへ公開するdescriptionやJSON schema registryを持たない。model-driven tool selectionを
  採用しないため、Agent宣言に`tools` fieldを追加しない。

#### 精査する(Evidence Reviewer)

- Evidence Reviewer Agent は1つの`ResearchTask`について、内部候補と外部候補を1つの候補列として
  受け取り、精査して根拠を見極める。役割は「選ぶ」ではなく「読んで精査し、採用できるものと
  足りないものを見極める」である。現行の`missing`はその見極めの一部であり、将来「足りないので
  再調査する」へ発展させたときも語彙が破綻しない。
- 改名を次で一式そろえる。半端に残さない。
  - stable name: `external_evidence_selector` -> `evidence_reviewer`
  - phase名: `external_selector` -> `evidence_review`
  - class: `EvidenceReviewer`
  - 出力型: `ExternalEvidenceSelectionDraft` -> `EvidenceReviewDraft`、
    `EvidenceSelectionResult` -> `EvidenceReviewerResponse`(selections + missing)
  span属性`agent_name`とmetric labelの値が変わることを受け入れる。
- `assessor` / `curator` / `analyst` / `auditor`は採らない。`assessment`(投資判断)、
  `curation`(本文整形)、`analysis`(記事分析)、`audit`(pipeline_events)が既存BCの語彙であり、
  同じ名前を別の概念へ流用しない。
- 1回のRunにおける「ユーザーが求めていること」の正本は`QuestionContext`ただ1つであり、Runの中で
  1回だけ準備される。各Agentはその正本から、自分の役割に必要な範囲だけを型付きinputとして受け取る。
  reviewerはこの正本を部分的に受け取る最初のconsumerである(planner / Direct Answer /
  Evidence Answerは現状いずれも全体を受け取る)。
- reviewerへ`content_requirements`(この回答に含めるべき内容)を渡す。`research_goal`はplannerによる
  圧縮であり、ユーザーが求めている内容を落とすためである。両者の関係をinstructionsで固定する。
  `research_goal`はそのtaskの担当範囲、`content_requirements`は回答が満たすべき内容を表し、
  担当範囲の中で、求められている内容に有用な候補を選ぶ。
- reviewerへ`standalone_question`を渡さない。質問文そのものではなく、質問から導出された
  「求めていること」を共有する。context preparationのfallback経路は`content_requirements`へ
  生の質問を格納するため、構造化に失敗して質問文しか情報がない状況では`content_requirements`
  経由で届く。
- `response_requirements`(回答の形式・深さ)、`relevant_prior_coverage`、`active_goal`は渡さない。
  根拠の選別に寄与せず、候補列に加えて入力量だけが増える。
- `content_requirements`はLLM成功経路では空になりうる。その場合reviewerは`research_goal`だけで
  判断する。`research_goal`は`min_length=1`のため判断材料がゼロにはならない。空を異常として
  扱わず、fallbackも設けない。
- `content_requirements`はdescriptionだけを渡し、`requirement_id`を渡さない。reviewerは
  requirement単位の充足を報告せず、idはmodel-visibleな判断材料にならない。
- 候補projectionは内部・外部で同じfield構成にし、単一のindex空間で通し番号を振る。
  reviewerはindexだけで候補を指定する。
- 候補projectionに出所種別(internal / external)を含めない。選別基準は`research_goal`と質問への
  適合だけであり、出所は判断材料ではない。使わせない値を渡してinstructionsで禁じる形にしない。
  fieldの言語や`source_name`の有無から出所が推測可能である点は受け入れる。出所による偏りが
  観測された場合は、選別後の決定的なpolicyとして呼び出し側で扱い、reviewerへ戻さない。
- 精査前の候補にtask間の重複排除を行わない。同じ記事が複数taskの候補に現れるのは目的ごとの
  収集として自然であり、各taskは自分の`research_goal`に照らして独立に精査する。同一記事が
  複数taskで採用され、別々の`claim`を持つことも正しい動作である。重複の解決は合流が行う。
- `content_requirements`は`sanitize_for_untrusted_block()`と`<untrusted_input>`境界を通す。
  候補本文と同じ扱いとし、信頼済みテキストとして展開しない。
- 候補projectionにURL、`assessment_id`、`curation_id`、内部記事の公開id、source refを含めない。
  出典メタデータはindexを鍵に呼び出し側が候補列から再構築する。
- 内部候補も外部候補も`sanitize_for_untrusted_block()`と`<untrusted_input>`境界を通す。
  内部記事の`summary`と`key_points`は自社パイプラインの生成物だが、元は外部記事本文由来であり、
  信頼済みテキストとして扱わない。
- 選別結果の`claim` / `why_selected` / `missing`のcapは現行値(300字 / 300字 / 5件×200字)を維持する。
  task単位の採用上限も現行値(5件)を維持する。
- 範囲外indexと重複indexは`AnswerEvidence.from_reviewer_response()`が決定的に不採用とする。
  reviewerの出力を無検証で信用しない。
- 選別済み根拠の中間`source_ref`は、`external-{task_index}-{candidate_index}`から出所非依存の
  採番へ変える。task内の候補列が内部・外部の統合index空間になり接頭辞が実態と合わなくなるためで
  ある。`task_index`による修飾は維持する。`candidate_index`はtaskごとに0から振られ、修飾しないと
  task間で衝突する。この`source_ref`は`build_answer_input_evidence()`が最終的な連番へ振り直すため
  ユーザーには露出せず、Run内部の整理番号として閉じている。
- reviewerが失敗したtaskは根拠ゼロで終わる。距離順やprovider rank順で根拠を捏造するfallbackを
  設けない。`claim`を持たない根拠を作らない。

#### 合流と回答

- 全taskの根拠をtask_index昇順で合流し、run単位で重複排除する。外部根拠はURL、内部根拠は
  内部記事の識別子で判定し、先に出たものを残す。
- `AnswerEvidence`には重複排除後の確定根拠だけを保持し、落ちた側の`claim`を統合しない。
  不採用件数はproduction contractへ含めない。
- 合流後の`AnswerInputEvidence`への正規化、`source_ref`の通し番号採番、回答Agentの入力契約、
  `cited_refs`の検証は変更しない。`missing_aspects`の組み立ては後述のとおり変更する。
- 内部根拠の本文は、reviewerが書いた`claim`と既存の`summary`(+`key_points`)を持つ。
  外部根拠の本文は`claim`と`snippet`を持つ。両者が`claim`を持つことで回答Agentが受け取る
  根拠の形式が揃う。

#### 完了しなかった調査の表明

- taskの収集失敗と精査失敗でRun全体を失敗にしない。他のtaskが根拠を出せていれば回答は返す。
  1本の調査が落ちたことを理由に、成立している根拠を捨てない。
- 完了しなかったtaskがある場合は`missing_aspects`へ1行加える。文言は固定とし、
  `research_goal`を含めない。ユーザーへ具体的な調査目的を見せるかは別タスクで判断する。
- 文言は「完了できなかった調査があります」とする。`_deduplicate()`が同一文字列をまとめるため、
  落ちたtask数によらず1行になる。「一部の」と書くと全taskが落ちた場合に事実と食い違う。
- `status`は`missing_aspects`から導出される既存規則をそのまま使う。`missing_aspects`が空でなければ
  `insufficient`になり、`AnswerQuestionResult`のvalidatorが「answeredなのに不足がある」
  「insufficientなのに何も挙げていない」の双方を構造的に禁じる。statusを個別に設定しない。
- `status`はAPI responseに含まれず、ユーザーに見えるのは`missing_aspects`の行だけである
  (frontendは「確認できなかった点」として列挙する)。statusは不完全さをRun内部で型として持つ役割に
  留まる。
- run単位の`collection_failures`(`["internal_search"]`)を廃止し、収集失敗の表現をtask単位へ
  一本化する。新設計では内部収集もtask単位で走るため、run単位のフラグは「1つでも落ちたら立てる」
  (成功したtaskがあるのに全体が失敗したと読める)も「全部落ちたときだけ立てる」(部分失敗が消える)も
  実態と合わない。
- `collection_failures`の廃止に伴い、経路名を出す文言(「内部記事検索を完了できませんでした」
  「外部検索を完了できませんでした」)を廃止する。time filter失敗の文言
  (「指定された公開期間を外部検索へ適用できませんでした」)は収集経路の失敗ではなく
  期間指定を適用できなかった事実の表明であり、廃止対象に含めず維持する。どちらの経路が落ちたかは運用者の関心であり、
  metricとspanで観測する。ユーザーへは「完了できなかった調査がある」という事実だけを伝える。
  新設計では片方の収集が落ちても他方の候補で精査を続けるため、経路名の情報価値はさらに下がる。
- `AnswerQuestionResult`のうち永続化されるのは`answer`(message content)、`missing_aspects`、
  `sources`(別テーブル)だけであり、`status`と`plan_summary`は保存されない。したがって
  `collection_failures`の廃止に過去データの読み出し互換性の問題は発生しない。

#### 観測と失敗分類

- task単位の実行内容と失敗分類を保持する。収集(内部 / 外部)と選別を別々のfieldで表現し、
  「どちらの収集が落ちたか」「選別まで到達したか」を区別できる状態にする。
- Agent phase spanは既存の`agent_phase`固定名と`agent_name` / `task_index`属性を維持する。
  Tool call spanの追加は本sliceで行わない。
- 回答Runのspan属性へ、内部・外部別の根拠採用数と、`cited_refs`との突き合わせによる引用数を出す。
  内部候補が無条件で根拠になる段3のうちに仕込み、段4の採用規則の変更が内部引用へ与える影響を
  測る分母にする。あわせて内部合流dedupで落ちた件数と、内部収集が失敗したtask数もspan属性で出す
  (内部側の報告がtask reportへ構造化される段4までの穴埋め)。載せるのは件数のみとし、
  本文非露出の制約を守る。
- 内部eventの`task_index`はrequiredとする。deploy窓ではRedisに残る旧形(内部event)が
  decodeで落ち、live UIの直近event表示が一時的に欠けるが、fail-softかつTTLで自然治癒するため
  許容する。
- `task_index`は、1回のRunで並列に走るresearch taskを区別する番号である。planの`research_tasks`
  における位置(0始まり)をそのまま使う。
- 番号は実行中だけでなく結果にも付いて回る。これにより次が成立する。
  - 完了順ではなく宣言順(0, 1, 2)へ並べ直せる
  - 各taskがちょうど1回reportしたことを検証できる
  - taskごとに0から振られる`candidate_index`を、`task_index`との組で一意にできる
  - 並列に流れるspanとeventが、どのtaskのものか判別できる
- Runの中だけで意味を持つ番号として扱う。DBへ保存せず、ユーザーへ露出させず、別のRunの同じ番号と
  比較しない。
- 内部検索eventに`task_index`を追加し、外部eventと同じ帰属情報を持たせる。frontendのparserは
  既知fieldだけを拾い未知fieldを無視するため後方互換であり、frontend側の追随を必要としない。
- 内部検索eventは収集がtask単位になることでRunあたり最大3回発火する。frontendは最新1件だけを
  表示するため件数が累積せずtask単位の値で入れ替わるが、実際に検索が起きている事実の表示として
  受け入れる。Run単位へ集約する機構を設けない。
- 外部向けevent名(`external_search.*`)は本sliceで変更しない。選別が外部専用でなくなり
  `external_search.evidence_selected`の名前は実態と食い違うが、frontendの表示文言は元から出所に
  言及しておらず、改名はSSE contractの破壊的変更として3段デプロイを要する。親仕様PR10の
  naming cleanupへ持ち越す既知の負債として扱う。
- span属性、event、status descriptionに質問本文、履歴、prompt、query text、candidate snippet、
  evidence本文、回答本文を載せない既存制約を維持する。
- `AnswerProgressStage`(`planning` / `retrieving` / `synthesizing`)の語彙と発火順序を変更しない。

### Non-goals

- modelがtoolや次のAgentを選択するagent loopを導入すること。Pythonによる明示的な順序と分岐を維持する。
- `openai-agents`をdependencyへ追加すること。
- 回答Agent(direct / evidence)のprompt、契約、streaming機構を変更すること。
- Question Context Agent、Input Safety Agent、planner以外のAgent宣言を変更すること。
- `source_ref`の最終採番規則、`AnswerQuestionResult`のshape、API response contractを変更すること。
- embedding cacheの配線、vector検索SQL、`InternalArticleSearchHit`の構造を変更すること。
- `answering_runner.py`に残る回答workflow以外の掃除(history投影の移設、汎用async配管の移設、
  `_plan_target_time_window`削除)。これらは本sliceと独立に実施してよい。
- Tool call spanの新設とusage観測の完成(親仕様PR10の残件)。

### Done

- `SearchPlan`が`ResearchTask`のlistを持ち、各taskが`research_goal`と`article_search_queries`を
  関連づけ、query合計が予算(3)で構造的に縛られている。
- Researcher が1 task分の内部候補と外部候補を集め、片方の失敗で他方を捨てない。
- Evidence Reviewer が内部候補と外部候補を同じ候補列として受け取り、`research_goal`に照らして
  選別し、内部根拠にも`claim`が付く。
- 1 taskの収集失敗・選別失敗が他のtaskの根拠を消さない。
- 進捗eventの発火がworkflow層(Researcher / runner)にあり、ToolとAgentは発火しない。
- `answering_runner.py`からexternal pipeline専用のprivate methodが消え、`run()`から
  safety → context → planning → 分岐 → task並列 → 合流 → 回答の順序を追える。
- 既存のregression(回答shape、citation検証、`missing_aspects`の組み立て、progress stage、
  resource lifecycle、text非露出)がすべて通る。

## 責任境界

| 責任 | Worker | AnsweringRunner | Researcher | Reviewer | Agent | Tool | Composition |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| persistent attempt取得 | ○ | - | - | - | - | - | - |
| planning起動と分岐 | - | ○ | - | - | 実行 | - | 配線 |
| task並列度policy | - | ○ | - | - | - | - | - |
| task内の収集順序 | - | - | ○ | - | - | - | - |
| 外部query生成 | - | - | 起動 | - | 実行 | - | 配線 |
| 外部HTTP検索 | - | - | 起動 | - | - | 実行 | 配線 |
| 内部vector検索 | - | - | 起動 | - | - | 実行 | 配線 |
| 候補pool構築 | - | - | ○ | - | - | - | - |
| 収集進捗eventの発火 | - | - | ○ | - | - | - | - |
| 根拠の選別と言語化 | - | - | - | 起動 | 実行 | - | 配線 |
| index→出典の再構築 | - | - | - | ○ | - | - | - |
| 選別進捗eventの発火 | - | ○ | - | - | - | - | - |
| task間の合流と重複排除 | - | ○ | - | - | - | - | - |
| citation検証とfinal assembly | - | ○ | - | - | - | - | - |
| external runtime scope | - | ○ | 利用 | 利用 | - | - | factory実装 |
| 完了・失敗の永続化 | ○ | - | - | - | - | - | - |

## 目標実行順

```text
AnsweringRunner.run
├─ Input Safety Agent
├─ Question Context Agent
├─ hook
├─ Question Planner Agent          → DirectAnswerPlan | SearchPlan
├─ DirectAnswerPlan → Direct Answer Agent
└─ SearchPlan
   ├─ target_time_window 解決(plan単位、失敗は外部収集だけを落とす)
   ├─ external runtime scope activate
   ├─ [ResearchTask ごとに並列、最大3]
   │  ├─ Researcher.collect(task)
   │  │  ├─ External Search Query Agent  → 英語keyword query 1〜3件
   │  │  ├─ External Search Tool         → 候補pool(最大20)   ┐並列
   │  │  └─ Internal Search Tool         → 内部候補           ┘
   │  └─ Evidence Reviewer Agent         → 選別 + claim + missing
   ├─ task間の合流と重複排除
   └─ Evidence Answer Agent
```

`AnsweringRunner`はtask単位のfan-outと合流を所有し、1 taskの中の手順はResearcherとReviewerが持つ。
`run()`から読めるのは上記の縦の並びであり、task内の詳細は追わなくてよい。

## 別タスクへ送る判断

本sliceでは扱わず、別スコープで判断する。

- `missing_aspects`の文言へ`research_goal`を含めるか。含める場合は表示経路に載る文字列となるため、
  `research_goal`(現在`max_length`なし)へ既存の`MISSING_ITEM_MAX_CHARS`(200)と同じcapが必要になる。
- 合流の重複排除で落ちた側の`claim`の統合。task reportに残るため、失われて困る事例が観測されて
  から判断する。
- `external_search.*` event名の出所非依存化(親仕様PR10のnaming cleanup)。
- task並列度まわりの内部名(`resolve_external_search_agent_count` / `requested_external_agent_count` /
  `ExternalSearchOutcome.effective_agent_count`等)の出所非依存化。段3以降、この並列度は内部収集を
  含むtask全体を縛るが、名前とreport上の置き場が外部検索専用のまま残っている。report再設計と
  同時でないと半端な改名になるため、段4以降で扱う。
- 精査済み根拠の置き場の平坦化。`EvidenceCollectionOutcome.internal_evidence`(平坦)と
  `external_search.evidence`(`ExternalSearchOutcome`内、Optional)は同じ「精査済み根拠」だが
  形と置き場が非対称で、正規化とΣ整合validatorに分岐を強いている。外部evidenceの引き上げと
  `ExternalSearchOutcome`の並列度policyへの縮退、および`ExternalResearchRuntime`(reviewer runtime
  を含む束)の名前の見直しを、上記の並列度改名と同じタスクで扱う。
- Tool call spanの新設とusage観測の完成(親仕様PR10)。

## Test contract

### Plan 契約

- `plan_from_draft()`がdraftの`research_tasks`から`ResearchTask`を構築し、`research_goal`の重複、
  task内の空白のみ・重複queryを排除し、task上限3を適用する。
- query合計が予算(3)を超えるdraftが、各taskの先頭queryからtask順のround-robinで3件へ決定的に
  trimされ、どのtaskも0件にならない。
- task間で同じqueryを持つdraftがdefectにならず許容される。
- `SearchPlan`がquery合計の予算超過を構造的に拒否する。
- `plan_type=direct_answer`で`research_tasks`が空でない draft を defect として拒否する。
- `plan_type=search`で`research_tasks`が空、またはいずれかのtaskの`article_search_queries`が空の
  draftを defect として拒否する。

### 収集

- 内部収集が失敗し外部候補だけが残ったtaskで、選別へ進み根拠を返す。
- 外部収集が全query失敗し内部候補だけが残ったtaskで、選別へ進み根拠を返す。
- `target_time_window`の解決に失敗したtaskで、外部収集を行わず内部候補だけで選別へ進む。
- 内部・外部とも候補ゼロのtaskで、reviewerを呼ばずに収集失敗として終わる。
- 1つのtaskの収集失敗が他のtaskの根拠に影響しない。
- 各taskの内部候補がそのtaskのqueryだけから作られ、他taskのqueryや候補と混ざらない。

### 選別

- reviewerへ渡す候補projectionにURL、`assessment_id`、`curation_id`、source refが含まれない。
- reviewerへ渡す候補projectionに出所種別のfieldが含まれない。
- reviewer inputに`content_requirements`のdescriptionが含まれ、`standalone_question` /
  `requirement_id` / `response_requirements` / `relevant_prior_coverage` / `active_goal`が
  含まれない。
- `content_requirements`が空のRunでも、reviewerが`research_goal`だけで選別を完了する。
- 内部候補の本文と`content_requirements`が`sanitize_for_untrusted_block()`を通っている。
- 同じ記事が複数taskの候補に現れたとき、精査前に除かれず、各taskが独立に採用できる。
- 範囲外indexと重複indexのselectionを決定的に不採用とする。
- reviewerの2 attemptが尽きたtaskが根拠ゼロで終わり、他のtaskの根拠を消さない。
- 収集または精査が完了しなかったtaskがあるRunで、`missing_aspects`に固定文言が1行だけ加わり、
  `status`が`insufficient`になる。落ちたtaskが複数でも行は1つに畳まれる。
- 全taskが完了しなかったRunで、Run自体はfailedにならず回答が返る。
- `AnswerQuestionResult`に`collection_failures`が存在せず、経路名を出す文言が
  `missing_aspects`へ現れない。
- 選別された内部根拠が`claim`を持ち、回答Agentの入力で外部根拠と同じ形式になっている。

### 合流

- 複数taskが同じURLの外部候補を選んだとき、task_index昇順で先勝ちの重複排除が働く。
- 複数taskが同じ内部記事を選んだとき、同じ規則で重複排除が働く。
- 重複排除後の件数が`AnswerEvidence`とRun reportで一致する。
- 合流後の`source_ref`採番、citation検証、`missing_aspects`の組み立てが現行と同じ結果になる。

### 進捗event

- Internal Search Toolの実装がprogress eventを発火しない。
- 呼び出し側が現行と同じ地点で発火する(開始時にstarted、成功時のみcompleted、失敗時は
  completedなし)。
- 内部検索eventが`task_index`を持ち、taskごとに発火する。
- direct pathで検索系eventが発火しない。

### Resource lifecycle

- direct pathでexternal runtime scopeをactivateしない。
- 収集失敗、選別失敗、想定外例外、cancelのいずれの経路でもexternal clientを解放する。
- task並列中の1 taskで未分類例外が起きたとき、兄弟taskをcancelして合流してから例外を送出する。

### Architecture boundary

- `answering_runner.py`がexternal search のpolicy定数、prompt、provider例外型をimportしない。
- Agent宣言に`tools` fieldが存在しない。
- Researcher と Reviewer がDB session、Redis、Taskiq、HTTP clientの生成を知らない。
- Internal Search ToolがSSEのprogress reporterを知らない。

## 実装順

behavior-preservingではないため、1 PRで通しきらず5段に分ける。各段でテストを緑に保ち、単独で
merge・deployできる状態を保つ。

段の性格を先に固定する。段2と段5は振る舞いを変えない。段1はplanner promptの改訂によりplanの
中身が変わる。段3は収集入力(候補プール)が変わる。段4は採用規則が変わる唯一の段である。
「内部記事が無条件で根拠になる」から「精査を通ったものだけが根拠になる」への変更を段4の差分
だけに帰属させるため、候補プールは段3で確定させ、段4には採用規則とユーザー可視のreport表現
以外の変更を入れない。

1. **Plan契約**: `ResearchTask`導入、`QuestionPlanDraft`とresponse schemaのnested化、
   planner promptの改訂(v4 → v5、予算配分の指示)、`plan_from_draft()`の再実装(task内正規化と
   予算のround-robin trim)。既存の収集経路は`ResearchTask`から従来形へ射影して動かす:
   全taskのqueryをtask順に平坦化し、casefold先勝ちで重複を除いて既存の内部検索へ
   (旧`SearchPlan`のrun単位一意性を射影側で保つ)、`research_goal`を既存の外部taskへ。
   合計≤3がvalidatorで保証されるため、平坦化は現行の`InternalSearchQueries`(上限3)にそのまま
   収まる。この射影がseamで、段3で消える。
2. **Internal Search Tool**: 内部検索をExternal Search Toolと同じ契約へ整形し、progress event
   の発火を`InternalSearchService`から呼び出し側へ移す。発火地点は現行の鏡写しとし、SSE契約を
   変えない。metricsはTool実装に残す。呼び出し側は`AnsweringRunner`のまま、run単位で1回。
3. **Researcher**: task単位の収集を`AnsweringRunner`から切り出し、並列の軸を「内部 ∥ 外部」から
   「task ∥ task」へ移す。reviewerはまだ外部候補だけを見る。内部候補はtask単位のまま無条件で
   根拠になり、候補プールの拡大(run単位top-5 → task毎top-5)がこの段で回答に出る。run単位の
   内部識別子による先勝ち重複排除をこの段の合流点に入れる(重複が初めて可能になる段でガードする。
   終状態の合流規則そのものであり捨てコードではない)。収集進捗eventの発火をResearcherへ移し、
   内部eventへ`task_index`を追加する(`/gen-types`もここ)。内部・外部別の採用数・引用数の
   span属性もここで仕込む。過渡期の`collection_failures`は「全taskの内部収集が失敗したときのみ
   `["internal_search"]`を立てる」とする(現行意味の最近似)。DB同時接続ピーク(1→最大3)、
   embedding API呼び出し(1→task数)、cancelと例外伝播、external scopeの解放が変わるのは
   この段である。
4. **Evidence Reviewer**: 候補列の統合、projectionの統一、prompt改訂、内部根拠への`claim`付与、
   失敗分類とreportの再設計、改名一式。`collection_failures`を廃止し、`missing_aspects`を
   固定文言へ置き換える。候補プールと重複排除は段3から変わらないため、根拠に関する差分は
   採用規則だけになる。
5. **掃除**: `_gather_search_branches()`等の旧経路、`collection_failures`の文言map、その他
   dead codeの削除のみ。振る舞いを変えない。

## 影響範囲

- `app/agent/planning/` — contract、prompts、service(draft finalize)
- `app/agent/evidence_collection/internal_search/` — Tool契約化、progress event発火の
  呼び出し側への移動
- `app/agent/evidence_collection/external_search/` — Researcher への再配置、reviewer関連の移動
- `app/agent/running/answering_runner.py` — external pipeline の削除、task fan-out へ置換
- `app/agent/answering/evidence_answer/evidence.py` — 内部根拠の本文組み立て
- `app/agent/answering/result_assembly.py` — task report からの `missing_aspects` 組み立て
- `app/agent/contract.py` / `app/schemas/research.py` — 内部検索eventへの`task_index`追加
- `app/agent/composition.py` — Internal Search Tool と Researcher / Reviewer の配線
- frontend — `/gen-types`による型再生成のみ。event名は変えず追加fieldはparserが無視するため、
  消費側(`live/events.ts`、`ActiveRunStatus.tsx`)の変更を伴わない

DB schema変更なし。API response shape変更なし。新規dependencyなし。

### 実装後に確認する運用値

- 回答Run 1件あたりのvector検索のDB同時接続ピークが、内部検索の直列実行による1から、
  並列task数(最大3)へ増える。agent workerは`pool_size 5 + max_overflow 5`(cap 10)と
  `--max-async-tasks 10`で構成されており、プール構成は本sliceで変更しない。
  各Runは実時間の大半をLLM待ちに費やすため同時に検索フェーズへ入る確率は低く、溢れた場合も
  `pool_timeout`による内部収集の失敗はbest-effort契約で外部候補による選別継続へ縮退する。
  実測して飽和が観測された場合にのみプール構成を見直す。
- query embedding API呼び出しが回答Runあたり1回から並列task数へ増える。待ち時間は並列のため
  増えないが、呼び出し回数はtask数に比例する。
- 内部根拠は「run単位で無条件に最大5件」から、段3で「task毎top-5の無条件採用(3 taskで
  最大15件)」、段4で「taskの採用枠5件を外部候補と競合する精査済み根拠」へ変わる。段3では
  回答Agentへ渡るevidence総量が増える(内部最大15+外部最大15)。回答promptの入力量と引用の
  挙動を観測する。
- 内部記事が引用されない回答の頻度を実測する。段3で仕込む採用数・引用数のspan属性が比較の
  分母になる。出所による偏りが観測された場合は、reviewerへ指示を足すのではなく、選別後の
  決定的なpolicyとして呼び出し側で扱う。
