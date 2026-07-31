# Agent 進捗 stage 語彙を実工程へ一致させる slice 仕様

更新日: 2026-07-31

実装状況: Draft

## 位置付け

本sliceは、run実行中の進捗を表す`progress_stage`の語彙を、AnsweringRunnerが実際に通る工程と
一対一に対応させる。新しい可視化機構を作るのではなく、既に配信・永続化されている値の名前と
粒度を実装に合わせる。

先行slice: `agent-run-progress-stage-slice.md`(3値の導入とDB/API契約)、
`agent-run-live-events-slice.md`(activityイベント)、
`agent-progress-forward-merge-slice.md`(poll/SSE合成の単調前進)。

`agent-run-progress-stage-slice.md`はNon-goalsで「progress_stageの語彙拡張(3値は親仕様合意。
粒度が足りない分はSlice 5の領分)」と定めていた。本sliceはその判断を更新する。粒度の問題では
なく、3値のうち2値が実工程のどれとも対応しない名前であることが問題だからである。

本sliceはPR1に相当する。activityイベント名の是正(PR2)とphase span属性の追随(PR3)は別sliceで
扱う。

## Work Definition

### Problem

- `retrieving` / `synthesizing`は工程名として何をしているか読めない。実装のどの語彙とも
  一致せず、moduleにもAgent宣言にもphase spanにも存在しない名前である。
- 実行される工程は6段だが、名前を持つのは3つだけである。セーフティチェック、コンテキスト整理、
  エビデンスレビューは外から見えない。
- エビデンスレビューは`retrieving`に吸収されている。Run単位1回の精査は
  `evidence-review-run-scope-slice.md`で独立した工程になったが、進捗表示の上では
  「情報収集中」のままである。実際にはtask並列の収集が終わった後、単一のLLM呼び出しで
  数秒から十数秒かかる区間であり、収集と混ぜて表示する理由がない。
- run開始からplanning報告までの区間が`progress_stage = NULL`になり、UIは「生成中」を出す。
  この区間にはセーフティチェックとコンテキスト整理という2工程が入っている。
- 語彙が3系統に割れている。stage(`planning` / `retrieving` / `synthesizing`)、
  phase span(`question_planning` / `external_query` / `evidence_review` / `evidence_answer` /
  `direct_answer` / `question_context`)、Agent名(`question_planner` / `evidence_reviewer`ほか)。
  同じ工程を指す語が複数あり、どれが工程名なのか決まっていない。

### Evidence

- stage語彙のLiteralは4箇所に重複記述されている。`app/agent/contract.py:45`(正本)、
  `app/agent/live_updates/stream.py:46`(SSEイベント)、`app/agent/runs/projection.py:29`、
  `app/schemas/research.py:76`。StrEnumは`app/agent/runs/types.py:24-27`、DB CHECK制約は
  `app/models/agent_run.py:65`と`alembic/versions/y1_agent_history.py:261`。
- 報告箇所は`AnsweringRunner`の3箇所(`answering_runner.py:163` / `:222` / `:201`と`:235`)。
  `_report_progress()`が`self._progress`のNoneを吸収する。
- SSEとDBは`AgentRunLiveStageReporter`が同じ値をfan-outする(`live_updates/reporters.py:32-36`)。
  `asyncio.gather(return_exceptions=True)`のため片方の失敗は他方に伝播しない。
- DB書き込みは`status = 'running'`かつ`attempt_epoch`一致の条件付きUPDATEで、全例外を捕捉して
  warning logのみ(`runs/progress.py:29-49`)。進捗の欠落はrunの成否に影響しない。
- 実行順は`AnsweringRunner.run()`にすべて集約されている。
  `input_safety_checker.check()`(`:135`) → `context_preparer.prepare()`(`:144`) →
  `planner.plan()`(`:172`) → `_collect_evidence()`(`:227`) → `reviewer.review()`(`:355`) →
  `direct_answerer.answer()`(`:202`)または`evidence_answerer.answer()`(`:236`)。
- 全taskの候補が内外ともゼロのRunは`reviewer.review()`を呼ばずに閉じる
  (`answering_runner.py:345-353`)。精査を実行しない経路が存在する。
- `ExternalSearchEvidenceSelectedEvent`は精査成功後に発火する(`answering_runner.py:372`)。
  現行のstage語彙では`retrieving`区間の出来事だが、6段化すると精査工程中の出来事になる。
- frontendのstage順序は`reducer.ts:197-207`の`stageRank`(null=0 / planning=1 / retrieving=2 /
  synthesizing=3)。`advanceResearchLiveStage()`が厳密前進のみ反映し、遅延到着した古いstageで
  巻き戻さない(`agent-progress-forward-merge-slice.md`の合成規則)。
- frontendのactivity表示はstageに依存する。`controller.ts:645-666`は`synthesizing`で非表示、
  `null`または`planning`で`question.resolved`、`retrieving`で検索系を選ぶ。
  `ActiveRunStatus.tsx:60-66`が同じ分岐を表示側に持つ。
- activityイベントの`type`はLogfireのmetricに載っていない。`projection_drop`も
  `reason="unknown_event"`固定である(`live_updates/metrics.py:80-81`)。stage語彙の変更は
  metricの時系列に影響しない。

### Invariants

#### 工程名は6値であり、実行順に単調前進する

- 語彙は`safety_check` / `context_resolution` / `planning` / `evidence_collection` /
  `evidence_review` / `answering`の6値とする。
- 正本は`app/agent/contract.py`の`AnswerProgressStage`。DB CHECK制約、`AgentRunProgressStage`、
  API schemaはこれと一致する。一致は宣言に留めず、集合として検証する。
- SSEイベントのstageは正本の型をそのまま使い、語彙を再宣言しない。
- 同一attemptの中でstageは後戻りしない。frontendの`stageRank`は実行順の1〜6とし、
  既存の厳密前進マージをそのまま使う。
- 工程をskipする経路が2つある。skipは前進として扱い、逆行としない。

  | 経路 | 遷移 |
  |---|---|
  | direct answer plan | `planning` → `answering`(4・5をskip) |
  | 全taskの候補ゼロ | `evidence_collection` → `answering`(5をskip) |

#### 報告の所有者はAnsweringRunnerである

- 6段すべてを`AnsweringRunner`が報告する。`InputSafetyChecker` / `QuestionContextPreparer` /
  `Researcher` / `EvidenceReviewer`に報告責務を移さない。stage意味論の所有者を一箇所に保つ
  という既存の設計判断を維持する。
- 各工程の実行直前に報告する。工程の完了を報告しない。
- 精査を実行しないRunでは`evidence_review`を報告しない。実行しなかった工程に入ったことを
  表示しない。

#### NULLは工程が永続化されていないことを表す

- `progress_stage = NULL`は単一の原因に絞れない。次のいずれもNULLになる。
  - attemptをacquireした直後で、最初の報告がまだ届いていない(`acquire_for_execution`が
    前attemptの残骸をNULLへ戻す)
  - `run()`へ入る前に失敗した(agent構築、Redis初期化、履歴読み取りなど)
  - policy blockedとして終端し、既存のredactionが値を消した
  - best-effortの書き込みがすべて失敗した
- `safety_check`がrunの最初の工程になるため、正常に進行するrunでNULLが観測される区間は
  attempt acquire直後の一瞬まで縮む。工程の外で死んだrunに工程を捏造しない。
- policy blocked時に`progress_stage`を消す既存のredactionは変更しない。
- frontendの「生成中」fallbackは残す。

#### 既存の進捗機構の性質を変えない

- 書き込みはbest-effortのままとする。失敗はwarning logのみで、runの成否に影響しない。
- 条件付きUPDATE(`status = 'running'`かつ`attempt_epoch`一致)を維持する。
- 終状態後も値を残す。failed runのどの工程で死んだかが読める。
- SSEとDBへのfan-out構造、polling間隔、Redis Streamのtransportを変更しない。

#### 移行はmigration 1本で行う

- 既存行を変換し、CHECK制約を6値へ差し替える。`planning`は不変、`retrieving`は
  `evidence_collection`へ、`synthesizing`は`answering`へ写す。
- downgradeでは`evidence_collection`と`evidence_review`を`retrieving`へ、`answering`を
  `synthesizing`へ戻す。`safety_check`と`context_resolution`はNULLへ落とす。旧語彙に対応する
  工程が存在しないため、`planning`へ丸めて実際には入っていない工程を名乗らせない。
- deploy中の数分間、旧コードが書く`retrieving` / `synthesizing`が新制約に弾かれることを
  許容する。書き込みはbest-effortであり、runは落ちず、影響は進捗表示が一時的に更新されない
  ことに限られる。expand / contractの2段には分けない。

### Non-goals

- activityイベント名の是正。`external_search.evidence_selected`が精査工程の産物である問題は
  PR2で扱う。本sliceではイベント名を変えず、表示条件の追随だけを行う。
- phase spanの`phase`属性を工程名へ揃えること(PR3)。
- metric名の変更。`vector.agent.answer_synthesis.outcome` /
  `vector.agent.internal_retrieval.outcome` / `vector.agent.planner.outcome`は工程単位ではなく
  Agent・機構単位の語彙であり、名前変更は時系列の断絶を伴う。
- Agent宣言の名前を工程名へ揃えること。1工程に複数Agentまたは非LLM機構が対応するため、
  工程名とAgent名は別軸のままとする。
- moduleディレクトリのrename(`input_safety/` / `question_context/`)。
- 新しい進捗情報を増やすこと。工程の所要時間、進行率、task単位の進捗は扱わない。
- polling間隔、SSE transport、attempt epochのfencing規則の変更。

### Done

- `AnswerProgressStage`が6値であり、DB CHECK制約・SSEイベント・API schemaがこれと一致している。
- `AnsweringRunner`が6工程それぞれの実行直前に報告し、direct answer経路と候補ゼロ経路で
  該当工程をskipする。
- 既存行の`retrieving` / `synthesizing`がmigrationで新語彙へ写り、CHECK制約が6値になっている。
- frontendが6値の順序を持ち、逆行しない既存の合成規則が保たれている。
- UIが6工程それぞれのラベルを表示し、「根拠N件を選別」が精査工程でも表示される
  (現行の表示内容が失われない)。
- 進捗の書き込みが届いたrunで、`safety_check`到達後に`progress_stage = NULL`が残らない。
  書き込みはbest-effortのままなので、すべて失敗したrunがNULLになることは変わらない。
- 既存のregression(attempt epochのfencing、best-effortの失敗吸収、終状態の不変、
  poll/SSE合成の単調性)がすべて通る。

## 工程と語彙の対応

| # | stage | 報告位置 | 実装 | UIラベル |
|---|---|---|---|---|
| 1 | `safety_check` | `check()`前 | `input_safety/` | 検証中 |
| 2 | `context_resolution` | `prepare()`前 | `question_context/` | コンテキストを整理中 |
| 3 | `planning` | `plan()`前 | `planning/` | 計画中 |
| 4 | `evidence_collection` | `_collect_evidence()`前 | `evidence_collection/` | 情報収集中 |
| 5 | `evidence_review` | `review()`前 | `evidence_collection/evidence_review/` | 情報を選別中 |
| 6 | `answering` | 各`answer()`前 | `answering/` | 回答作成中 |

UIラベルのうち`計画中` / `情報収集中` / `回答作成中`は現行の文言を維持する。残る3つは新規。

## 影響範囲

### backend

- `app/agent/contract.py` — `AnswerProgressStage`を6値へ
- `app/agent/runs/types.py` — `AgentRunProgressStage`を6値へ
- `app/agent/live_updates/stream.py` — SSE stageイベントを正本型`AnswerProgressStage`へ統合
- `app/agent/runs/projection.py` — `ResearchProgressStageValue`とmatchの6 case化
- `app/schemas/research.py` — `ResearchProgressStage`
- `app/models/agent_run.py` — CHECK制約
- `app/agent/running/answering_runner.py` — 報告を3箇所から6箇所へ
- `alembic/versions/` — 新規1本(既存値の変換 + CHECK差し替え)
- `scripts/seed_e2e_research.py` — seedの`synthesizing`(5箇所)

`app/agent/runs/progress.py`と`app/agent/live_updates/reporters.py`は型経由で追随し、
リテラルを持たないため変更しない。

### frontend

- `src/features/research/live/events.ts` — `STAGES`定数
- `src/features/research/live/reducer.ts` — `stageRank`を1〜6へ
- `src/features/research/live/controller.ts` — `isProgressStage`と
  `latestRelevantPollingActivity()`のstage依存分岐
- `src/features/research/components/ActiveRunStatus.tsx` — ラベル6つとactivity表示条件
- `src/types/*.gen.ts` — `/gen-types`で再生成

### テスト

backend 14ファイル、frontend 9ファイル。多いのは
`tests/agent/test_router_research.py` / `tests/agent/test_agent_run_task.py` /
`tests/agent/runs/test_progress.py` / `tests/agent/answering/test_orchestration.py` /
`src/features/research/live/controller.test.ts`。

## Test contract

### 報告と順序

- evidence経路で`safety_check` → `context_resolution` → `planning` → `evidence_collection` →
  `evidence_review` → `answering`の順に報告される。
- direct answer経路で`planning`の次が`answering`になり、`evidence_collection`と
  `evidence_review`が報告されない。
- 全taskの候補がゼロのRunで`evidence_review`が報告されず、`evidence_collection`の次が
  `answering`になる。
- 精査が失敗したRunでも`evidence_review`は報告済みであり、`answering`へ進む。
- reporterがNoneのとき従来どおり動作する。

### 語彙の一致

- `AnswerProgressStage`、`AgentRunProgressStage`、`ResearchProgressStage`、
  `ResearchProgressStageValue`、DB CHECK制約が同じ6値の集合を持つ。1箇所だけ変えた状態を
  検出できる。

### 永続化と配信

- 6値それぞれがDBへ書かれ、CHECK制約に弾かれない。
- 旧語彙(`retrieving` / `synthesizing`)がCHECK制約に弾かれる。
- completed / failedのrunにstageが書かれない(条件付きUPDATEの維持)。
- attempt epochが一致しない報告が書かれない。
- SSEの`event: stage`に6値が載り、frontendのparserが受理する。

### migration

- `retrieving`の行が`evidence_collection`へ、`synthesizing`の行が`answering`へ写る。
- `planning`の行が変わらない。
- 変換後にCHECK制約違反の行が存在しない。

### frontend

- 6値それぞれのラベルが表示される。
- 遅延到着した古いstageで表示が巻き戻らない(既存の前進マージが6値でも成立)。
- `evidence_collection`から`answering`への直接遷移が前進として反映される。
- `evidence_review`中に`external_search.evidence_selected`が届いたとき「根拠N件を選別」が
  表示される。
- `answering`中はactivityが表示されない。
- `context_resolution`中に`question.resolved`が表示される。
- `progress_stage`がnullのrunで「生成中」fallbackが出る。

## 実装順

1. **backend語彙**: `AnswerProgressStage`を6値へ広げ、`AgentRunProgressStage` / SSEイベント /
   projection / schema / CHECK制約を追随させる。`AnsweringRunner`の報告を6箇所にする。
2. **migration**: 既存値を変換し、CHECK制約を差し替える。
3. **frontend**: `/gen-types`で型を再生成し、`STAGES` / `stageRank` / activity表示条件 /
   ラベルを追随させる。

段1と段2は同一PRに含める。段1だけをmergeするとCHECK制約が旧語彙のままで新値が書けず、
段2だけをmergeすると旧コードが新制約に弾かれるためである。

## 実装後に確認する運用値

- runningのまま`progress_stage = NULL`が続くrunの割合。原因は`run()`到達前の失敗、
  報告位置の漏れ、best-effortの書き込み失敗のいずれかであり、値だけでは切り分けられない。
  常態化している場合はwarning log(`agent_run_progress_update_failed`)の有無で判別する。
- `evidence_review`区間の滞在時間。収集と分離した結果、精査がどれだけ待ち時間を占めるかが
  初めて観測できる。
- `safety_check`の表示が実用上ちらつきとして問題にならないか。問題になる場合はUI側で
  表示を抑える判断を別途行う(stage値としては保持する)。
