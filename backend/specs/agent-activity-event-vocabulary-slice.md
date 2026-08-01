# Agent 進捗 activity イベント語彙を工程へ揃える slice 仕様

更新日: 2026-08-01

実装状況: Draft

## 位置付け

先行slice `agent-progress-stage-vocabulary-slice.md`(#88 merge済み)がstage語彙を実工程6値へ
移した。本sliceは同じ工程語彙をactivityイベント名へ広げ、併せて精査の選別イベントを
Run単位1本へ畳む。

先行sliceはNon-goalsで「activityイベント名の是正はPR2で扱う。本sliceではイベント名を変えず、
表示条件の追随だけを行う」と境界を切った。本sliceがその続きにあたる。phase spanの`phase`属性を
工程名へ揃えること(PR3)は別sliceとする。

## Work Definition

### Problem

- activityイベント名が工程を名乗っていない。stageは工程名になったが、activityは機構名
  (`internal_search` / `external_search`)と対象名(`question`)が混在した別系統のままである。
  同じ実行を説明する2つの語彙が並んでいる。
- `external_search.evidence_selected`は精査工程の産物なのに外部検索を名乗る。実際には内部記事の
  採用件数も含んでおり、名前と中身が一致していない。
- 精査はRun単位1回になったが、選別イベントだけがtask_indexごとに数え直されて再分解発火する。
  Runで1回起きたことが、taskの数だけ起きたように見える。

### Evidence

- 定義は`app/agent/contract.py`の6クラス、API schemaは`app/schemas/research.py`の6クラス。
  type文字列は両者で重複記述されている。
- 発火箇所は3つ。`evidence_collection/researcher.py`が収集系4種をtask単位で、
  `running/answering_runner.py:469-491`が選別をtask単位で、`running/hooks.py`が
  `question.resolved`をRun単位で発火する。
- 選別イベントは`EvidenceReviewOutcome`のinternal / external evidenceをtask_indexごとに
  数え直して組み立てられる。精査自体は`answering_runner.py:357`でRun単位1回である。
- activityはDBに永続化されない。Redis Stream(TTL 15分)とRedis list(50件・TTL 15分)だけを通る。
- 未知のtypeは両側で捨てられる。backendの`_decode_event`はvalidate失敗で`None`、frontendの
  `parseResearchLiveActivity`は`"unknown"`で`null`になる。
- activityのtypeはLogfireのmetricに載っていない。`projection_drop`も`reason="unknown_event"`
  固定である。改名の影響範囲はfrontend契約に閉じる。
- frontendの消費は`live/events.ts`(パーサとdiscriminated union)、
  `components/ActiveRunStatus.tsx`(表示文言)、`live/controller.ts`(stage別の選択)。

### Invariants

#### イベント名は工程を名乗る

- 6イベントすべてに`<工程>.`のプレフィクスを付ける。工程名はstage語彙と同じ値を使う。
- 規則は`<工程>.<機構>_<動作>`とする。工程に機構が1つしかない場合は機構名を省き、
  `<工程>.<動作>`とする。`evidence_collection`が機構名を持つのは内部検索と外部検索の
  2機構があるためであり、機構が1つの工程で機構名を書かない。

#### 精査の選別はRun単位1本である

- 選別イベントから`task_index`を外し、Run全体の採用件数を1本だけ発火する。精査がRun単位1回で
  ある以上、その結果もRun単位1本で表す。
- 収集系(検索の開始・完了、クエリ生成、候補取得)はtask単位を維持し`task_index`を保持する。
  taskごとに異なる検索をしており、件数を分けて出すこと自体に情報がある。

#### 将来の再検索と衝突する語を今消費しない

- `selected`は起きた事実を表し、工程が終わったかどうかの判断を含まない。精査の後に再検索へ
  回る設計が入っても意味が変わらない。
- `completed`と`retry_requested`は、将来「精査が確定した」「不足なので再検索へ回す」を表す語
  として空けておく。今回どちらも使わない。
- ラウンド番号をpayloadに入れない。ラウンドという概念が存在しないうちは表現しない。必要に
  なった時点でpayloadへ追加する(前方互換)。

#### 配信機構と表示を変えない

- SSE、Redis list、TTL、保持件数、stage別の表示条件を変えない。
- 表示文言を変えない。
- DBに残らないため移行は不要。deploy中にRedisへ残る旧名イベントは、新旧どちらのコードでも
  未知typeとして捨てられる。

### Non-goals

- phase spanの`phase`属性を工程名へ揃えること(PR3)。
- metric名、Agent宣言名の変更。工程単位ではなくAgent・機構単位の語彙であり、別軸のままとする。
- 再検索ループの実装。本sliceは名前が将来それと衝突しないことだけを保証する。
- 新しいactivityの追加、既存payloadへの項目追加。
- stageラベルとactivity行の重複解消(「情報を選別中」の下に「根拠N件を選別」が出る点)。
  文言の設計であり、語彙の一致とは別の判断になる。
- polling間隔、SSE transport、attempt epochのfencing規則の変更。

### Done

- 6イベントすべてが工程プレフィクスを持ち、stage語彙と同じ工程名を使っている。
- 選別イベントがRun単位1本で発火し、`task_index`を持たない。`evidence_count`はRun全体の
  採用件数である。
- 収集系イベントがtask単位のまま`task_index`を保持している。
- API schemaとfrontendの型が新名で一致し、production codeから旧名が消えている。
- 表示文言と表示条件が変わっていない。
- 既存のregression(SSE配信、recent eventsの読み出し、stage別の表示選択、未知typeの破棄)が
  すべて通る。

## 語彙の対応

| 現行 | 新 | 発火単位 | payload |
|---|---|---|---|
| `question.resolved` | `context_resolution.question_resolved` | Run 1回 | `standaloneQuestion` |
| `internal_search.started` | `evidence_collection.internal_search_started` | taskごと | `taskIndex` / `queryCount` |
| `internal_search.completed` | `evidence_collection.internal_search_completed` | taskごと | `taskIndex` / `hitCount` |
| `external_search.queries_generated` | `evidence_collection.external_search_queries_generated` | taskごと | `taskIndex` / `queries` |
| `external_search.candidates_fetched` | `evidence_collection.external_search_candidates_fetched` | taskごと | `taskIndex` / `candidateCount` |
| `external_search.evidence_selected` | `evidence_review.selected` | **Run 1回**(変更) | `evidenceCount` |

`evidence_review.selected`だけが機構名を持たない。この工程の機構はreviewer 1つであり、
動作だけで一意に読めるためである。`context_resolution.question_resolved`は動作だけにすると
何が解決したのか読めなくなるため対象を残す。

## 影響範囲

### backend

- `app/agent/contract.py` — 6クラスのtype。`ExternalSearchEvidenceSelectedEvent`は
  クラス名も工程に合わせ、`task_index`を落とす
- `app/agent/running/answering_runner.py` — `_report_selected_evidence_events()`をRun単位
  1本の発火へ縮小する。task別の数え直しが不要になる
- `app/schemas/research.py` — 6クラスのtypeとクラス名、選別イベントの`task_index`削除

`app/agent/live_updates/`は`AnswerProgressEvent`をそのまま包むため変更しない。

### frontend

- `src/features/research/live/events.ts` — パーサとdiscriminated union
- `src/features/research/components/ActiveRunStatus.tsx` — `switch`のcase名
- `src/features/research/live/controller.ts` — stage別の選択で参照するtype
- `src/types/*.gen.ts` — `/gen-types`で再生成

### テスト

backend 13ファイル、frontend 4ファイル。多いのは
`tests/agent/live_updates/test_recent_events.py` /
`tests/agent/evidence_collection/test_researcher.py` /
`tests/agent/running/test_external_pipeline.py` /
`tests/agent/running/test_retrieval_dispatch.py` /
`src/features/research/live/controller.test.ts`。

## Test contract

### イベント名

- 6イベントのtypeが新名である。
- 旧名のtypeを持つpayloadが未知として捨てられる(backendのstream decode、frontendのパーサ)。

### 発火単位

- 精査成功時に選別イベントが1本だけ発火する。候補があったtaskが複数あっても1本である。
- 選別イベントの`evidence_count`がRun全体の採用件数(内部+外部)と一致する。
- 精査を実行しないRun(全taskの候補ゼロ)で選別イベントが発火しない。
- 精査が失敗したRunで選別イベントが発火しない。
- 収集系イベントがtaskごとに発火し、`task_index`を保持する。

### 表示

- 新名のイベントで現行と同じ文言が出る。
- 選別イベントが1本になっても「根拠N件を選別」が表示される。
- stage別の表示条件(`evidence_collection`と`evidence_review`で検索系、`answering`で非表示、
  収集前で`question_resolved`)が変わらない。

## 実装順

1. **backend**: `contract.py`の6クラスのtypeを新名にし、選別イベントから`task_index`を外す。
   `answering_runner.py`の発火をRun単位1本へ縮小する。`schemas/research.py`を追随させる。
2. **frontend**: `/gen-types`で型を再生成し、パーサ・union・表示文言・stage別選択を追随させる。

同一PRに含める。API schemaが変わるため型の再生成が必要であり、段1だけをmergeするとfrontendが
未知typeとしてすべて捨てる状態になる。

## 移行

DB変更なし、migration不要。activityはRedisにしか存在しない。

deploy直後、旧コードが書いた旧名イベントがRedisに最大15分残る。新コードはこれを未知typeとして
捨てるため、その間に開始済みだったrunでactivityの詳細行が一時的に出ないことがある。stageの表示は
影響を受けない。
