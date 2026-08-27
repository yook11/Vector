# Research Handoff Spec

Status: Draft
Updated: 2026-08-28
Scope: Run が次の Run へ引き渡す調査の申し送りを thread 単位で持ち、planner prompt へ投影する案

## Problem

現在、Run をまたぐ調査文脈は 2 つに割れている。

- `AnswerBrief`: Run 冒頭で LLM を 1 回叩いて作る質問の整理。永続化されず、`RunResult.answer_brief` は
  queue 側で読まれていない。
- `ResearchCheckpoint`: Run 末尾に決定的に組み立てて `agent_runs.research_checkpoint` へ保存し、
  次 Run で直近 3 件を planner prompt へ並べる。

この構成では、質問の言い換えのために回答経路の先頭で LLM を 1 ホップ使う一方、
「次の調査が何を踏まえるべきか」という判断はどこにも残らない。checkpoint は Run 3 件を素で
展開するため prompt も膨らみやすい。

## 方針案

Run 冒頭の `question_context` 工程を畳み、代わりに Run 末尾で確定する `ResearchHandoff` を
thread 単位で 1 本持つ。質問は言い換えず、生の質問と直近履歴をそのまま planner へ渡す。
LLM 呼び出し回数は変えず、回答までの経路から 1 ホップ外すことを狙う。

## ResearchHandoff の内訳

台帳と整理を分け、層ごとに生成手段と失敗時の挙動を変える。

| 層 | フィールド | 中身 | 生成 |
|---|---|---|---|
| 台帳 | `runs` | Run ごとの `research_goal` と実行した query | 既存成果物からコピー |
| 整理 | `collected_overview` | これまでにどういう記事が集まっているか | LLM が毎回書き直す |
| 整理 | `unresolved_points` | 要望に対して何が得られていないか。検索したが得られなかったことを含む | LLM が毎回書き直す |
| 整理 | `next_search_guidance` | 次の調査で気をつけること、どう検索すると効果的か | LLM が毎回書き直す |

```python
class ResearchTaskRecord(BaseModel):  # 1 task 分の台帳
    research_goal: str                            # max RESEARCH_GOAL_MAX_CHARS
    executed_queries: tuple[_ExecutedQuery, ...]  # min 1, max EXTERNAL_TASK_QUERY_LIMIT


class ResearchRunRecord(BaseModel):   # 1 Run 分の台帳
    as_of: AwareDatetime
    tasks: tuple[ResearchTaskRecord, ...]         # min 1, max RESEARCH_TASK_LIMIT


class ResearchHandoff(BaseModel):
    schema_version: Literal[1] = 1
    updated_at: AwareDatetime
    runs: tuple[ResearchRunRecord, ...]           # 古い順、min 1、上限なし
    collected_overview: _ConsolidatedText = ""
    unresolved_points: _ConsolidatedText = ""
    next_search_guidance: _ConsolidatedText = ""
```

台帳は Run 単位のまとまりを保つ。平坦に積むと調査時点を記録ごとに持てず、
「実行済み query は鮮度の再確認が目的の場合を除き繰り返さない」という planner の
規則が判断できなくなる。

台帳が `research_goal` と `executed_queries` だけを持つのは、この 2 つが planner の規則が
直接読む事実であり、LLM に畳ませると同じ検索を繰り返させるため。採用した claim は
`collected_overview` へ、reviewer の missing は `unresolved_points` へ移る。Run ごとに素で
積むより、thread 全体で 1 本へ畳んだほうが短く、次の Run が読みやすい。

`schema_version` は永続化される `ResearchHandoff` だけが持つ。入れ子のノードにも版を置くと、
形が変わったときにどちらを上げるかの規則が無いまま版が 2 つ並ぶ。`ResearchRunRecord` の
`schema_version` は `ResearchCheckpoint` が単体で保存されていた頃の名残であり、ここで落とす。

整理は Run ごとに積まず、常に 1 本を上書きする。各工程の成果物は Run 単位であり、
handoff が要るのは thread 単位であるため、この工程の仕事は Run から thread への畳み込みになる。
reviewer の missing を LLM へ通し直すのは、reviewer と同じ仕事のやり直しではなく、
Run ごとに 1 本ずつ出るものを thread 全体で 1 本へ畳むため。

`unresolved_points` は reviewer の missing を並べ直すのではなく、thread 全体で要望に対して
何が得られていないかへ畳む。reviewer の missing は 1 Run の `research_goal` に対する未確認で
あり、thread をまたぐ要望に対して何が欠けているかとは粒度が違う。検索したが得られなかった
こと、検索自体が失敗したことも、ここに含める。

`next_search_guidance` は、この thread で検索してみて分かった経験則を残す。どの語で引くと
当たったか、どの方向は掘っても出なかったか、次はどこに気をつけるか。何を調べるかを決めるのは
planner の仕事であり、この項目は判断の材料になる観測を渡すだけで、計画そのものは持たない。

### 上限

台帳の積み上げには上限を置かない。コンテキスト管理が概念として固まっていないため、
どこで切るべきかを今は決められない。台帳は search Run ごとに単純追記する。

整理は 3 つとも 600 字を上限とする。thread 全体を 1 本へ畳んだ結果であり、Run 数によらず
一定に保つ。

要素ごとの文字数は既存の正本に従う(`research_goal` 200 字、query 200 字)。
1 Run 分の台帳は task 3 件が上限。

トレードオフとして、thread が長いほど台帳が伸びる。2026-08-28 の実測
(handoff 無しの planner input は 121 字):

| 積み上げ | 現実的 | 最悪 |
|---|---|---|
| 1 Run 分 | 659 | 4,624 |
| 3 Run 分 | 1,085 | 9,710 |
| 5 Run 分 | 1,511 | 14,796 |
| 10 Run 分 | 2,576 | 27,511 |
| 20 Run 分 | 4,706 | 52,941 |

整理は Run 数によらず一定なので、伸びるのは台帳だけになる。claim と missing を
整理側へ畳んだ結果、3 Run 分は置き換え前の checkpoint 3 件(6,069 字)の 1/5 以下、
20 Run 積んでも置き換え前の 3 件を下回る。

## 生成タイミング(案)

建て直すのは調査を行った Run(`search` plan)だけとする。`direct_answer` Run は
handoff を触らない。

- answering 工程に入る時点で整理の生成を並行起動する。この時点で plan・実行 query・
  収集ヒット・採用 evidence・reviewer missing は揃っており、回答本文を待つ理由がない。
- Run 末尾で結果を待ち合わせ、`complete_run()` と同一トランザクションで確定する。
  attempt_epoch の競合で Run 完了が成立しなければ handoff も書かれない。
- 停止(`AnswerGenerationStopped`)では並行タスクを cancel し、何も書かない。
- 整理の生成が失敗した場合は、台帳だけ更新し、整理は前回の値を据え置く案とする。
- 台帳を組み立てられなかった Run(精査失敗、記録可能 task 0 件)は handoff を触らない。

整理への入力は次を想定する。

- 前回の `collected_overview` / `unresolved_points` / `next_search_guidance`
- 今回の質問
- task ごとの `research_goal`、`executed_queries`、外部収集の失敗分類
  (`query_generation_failed` / `provider_failed`)
- ヒットした記事の title・出典・日付。採用されなかったものも含む
- 採用した evidence の `claim` と `why_selected`
- reviewer の `review_missing`

記事本文は渡さない。reviewer が本文込みで見た結果が `claim` と `why_selected` に
入っており、二度読ませる理由がない。採用されなかった記事の title を渡すのは、
何が手に入る状態だったかが採用結果だけからは読めないため。

外部収集の失敗分類を入力に含めるのは、「検索は通ったがその情報を報じた記事が無かった」と
「そもそも検索が失敗した」を次の Run が区別できるようにするため。前者は再検索しても
出ない見込みが高く、後者はやり直す価値がある。収集が 0 件で終わった Run は reviewer が
走らず `review_missing` が空で返るため、この分類がないと両者が同じ顔になる。

台帳は入力として見せるだけで、書き換えさせない。LLM の構造化出力は整理 3 本だけとし、
台帳は工程が決定的に積む。

model は `question_context` の設定を据え置き(gemini-2.5-flash-lite / temperature 0.1 /
max_output_tokens 1024)、prompt version は `v1` から始める。

## 保存先(案)

`agent_threads` に `research_handoff` JSONB を 1 列持ち、上書き更新する。
正本は常に最新の 1 本であり、Run 側に置くと「最新の completed run のもの」を探す
間接参照になるうえ、読まれない過去行が溜まり続けるため。所有権も thread 行の `user_id` で
直接効き、現在必要な `AgentRun → AgentThread` の join が要らなくなる。
既存の `agent_runs.research_checkpoint` は役目を失う。

上書きの競合は起きない。1 thread の active Run は `uq_agent_runs_thread_active` で同時 1 本に
制限されており、`complete_run()` の attempt_epoch fencing も同一トランザクションで継承される。

推移の追跡が要る場合は recorder / span 側で残す方向を想定する。

DB schema の変更を伴うため、着手前に別途合意が要る。

### 移行(案)

`agent_runs.research_checkpoint` の drop は別 PR に分ける。

1. 完了。`agent_threads.research_handoff` を追加し、書き込みと読み出しを切り替えた。
   旧列は書かれも読まれもしないまま残っている。
2. 新経路が安定してから、別 PR で旧列を drop する。

drop は取り消せないため、切り戻しが migration なしで済む状態を先に確保する。
既存 checkpoint から handoff を作り直す backfill は行わない。handoff は次の `search` Run で
建て直されるため、失われるのは移行直後の 1 Run ぶんの調査文脈に限られる。

台帳から `adopted_claims` と `unresolved_after_search` を外すと `ResearchHandoff` の形が
変わるため、既に書かれている handoff は `recall_research_handoff()` の検証に落ちて `None`
になる。列の増減はないため migration は伴わない。失われるのは同じく 1 Run ぶんに限られる。

## Prompt 投影(案)

planner 向けの投影関数 1 本だけを置き、prompt 側は差し込むだけにする。

```python
def render_planning_instruction(handoff: ResearchHandoff | None) -> str:
    """planner prompt へ差し込む文脈。空の handoff では空文字を返す。"""
```

台帳(Run ごとの `research_goal` と実行 query)と整理 3 本を 1 つの節へ描く。
回答工程向けの投影関数は作らない。prompt version は planner v10。

## 不変条件(案)

- ResearchHandoff は調査計画の文脈であり、回答の事実根拠ではない。引用は今回の Run の
  evidence からのみ出る。投影関数を planner 向け 1 本に限ることで構造的に保つ。
- 台帳の値は上流で正規化済みの値をそのままコピーし、切り詰め・再正規化を行わない。
  上限違反は Pydantic validation が拒否する。
- 整理は台帳を書き換えない。LLM の構造化出力に台帳を含めないことで構造的に保つ。
- 整理の生成失敗は Run を失敗させない。台帳だけが更新され、整理は前回の値が残る。
- 読み出した JSONB は投影前に必ず検証し、無効なら空として扱う。

## 既存工程への影響(案)

- `question_context` package と `AnswerBrief` を廃止する。planner と answerer は生の質問を受け取る。
- 指示語解決のため、直近履歴を要約せず planner の入力に置く。
- `research_checkpoint` package は ResearchHandoff の台帳へ吸収する。
- `AgentPhase` に `research_handoff` を追加する。工程名が成果物の型名 `ResearchHandoff` と
  DB 列 `agent_threads.research_handoff` と同語になる。他の工程は `planning` → `QuestionPlan` の
  ように工程名と成果物名を分けており揃っていないが、エージェント構成の見直しで動く見込みが
  高いため据え置く。
- `AnswerProgressStage` から `context_resolution` を落とす(ステップ 5)。整理の生成は
  回答確定前の後処理であり、進捗 stage としては見せない。

## 実装ステップ(案)

`AnswerBrief` の `active_goal` と handoff の整理が同じ planner prompt に
同時に乗る期間を作らないため、撤去を先に置く。

1. 完了。`question_context` と `AnswerBrief` を撤去し、planner と answerer が生の質問と
   直近履歴を直接受け取る。履歴の cap / normalize は `app/agent/threads/history.py` が持ち、
   描き方は各工程が持つ。prompt version は planner v8 / direct answer v5 / evidence answer v9。
   `prior_research`(checkpoint)は現状のまま。stage 語彙の縮小はステップ 5 へ分けた。
2. 完了。`ResearchHandoff` の記録層で checkpoint を置き換えた。型は
   `app/agent/contract.py`、builder / recall / 投影は `app/agent/research_handoff/`。
   `agent_threads.research_handoff` を migration `z16_thread_research_handoff`(expand)で
   追加し、`complete_run()` が thread 行の `FOR UPDATE` 下で上書きする。
   planner prompt version は v9、節名は `# Research Handoff`。
   旧 package は削除済み、旧列は書かれなくなったが残る。
3. 完了。整理(`collected_overview` / `unresolved_points` / `next_search_guidance`)を
   `ResearchHandoffOrganizer` が書き直すようにし、台帳から `adopted_claims` と
   `unresolved_after_search`、claim 合計の validator、`ResearchRunRecord.schema_version` を
   落とした。`app/agent/research_handoff/` は概念ごとに分け、`handoff_input.py`
   (上流の成果物から整理へ見せる分だけを投影する `ResearchHandoffInput.from_run()`)、
   `organized.py`(LLM の draft と整理 3 本への正規化)、`ledger.py`(台帳の組み立て)、
   `service.py`(工程と Protocol)を置く。記録は
   `app/agent/recording/research_handoff.py`。answering と並行起動し、Run 末尾で
   `HANDOFF_ORGANIZE_TIMEOUT_SECONDS` を上限に待ち合わせる。
   planner prompt version は v10。列の増減がないため migration は伴わない。
   `AgentPhase` に `research_handoff` を足すにあたり、`AnswerProgressStage` との集合一致を
   要求していた不変条件を包含関係へ緩めた(正本は
   `backend/specs/agent-phase-span-vocabulary-slice.md`)。
4. 旧列 `agent_runs.research_checkpoint` の drop。package はステップ 2 で削除済み。
5. `AnswerProgressStage` / `AgentPhase` / `AgentRunProgressStage` / API schema / CHECK 制約から
   `context_resolution` と、#191 以降到達不能な `safety_check` をまとめて落とす。
   alembic migration(CHECK 縮小と既存行の NULL 化)、frontend の stage 語彙、`/gen-types` を伴う。
   ステップ 1 の後は `context_resolution` が報告されなくなるだけで、
   `AgentRunProgressWriter` は best-effort、frontend は monotonic merge のため害はない。

ステップ 1 から 3 の間、スレッドの目的を持つフィールドが不在になることは受け入れる。

検証は planner input の組み立て箇所で、履歴が欠落なく写ることを見る程度に留める。
LLM が指示語を解決できたかは prompt 側の責務であり、テストで固定しない。

## Non-goals

- claim と query を紐付けるための収集・精査の改造。
- 回答の書き方に関わる継続要望を回答工程へ届けること。これは調査の申し送りとは別概念であり、
  切り出す判断ごと今回のスコープ外とする。
- スレッドが継続して追っている問いを LLM に立てさせること。生の質問と直近履歴から planner が読む。
- `direct_answer` Run で handoff を建て直すこと。
- checkpoint の履歴を Run 単位で保持し続けること。

## 未決事項

- 積み上げた台帳をどこで切るか。コンテキスト管理の概念が固まってから決める。
- 整理 3 本のフィールド名。`next_search_guidance` は前回案の `next_directives` から
  「方針を立てる」含みを外した語だが、確定ではない。
- 精査に失敗した Run で台帳を残すかどうか。現在は残さないが、query は実行済みであり、
  記録しないと次の Run が同じ query を繰り返しうる。
- 工程名 `research_handoff` が成果物の型名・DB 列名と同語である点。工程の Protocol は
  `ResearchHandoffOrganizer`、具象は `ResearchHandoffService` として役割を名前へ出したが、
  phase 名と型名は同語のまま。エージェント構成の見直しに合わせて再検討する。

## Done の目安

- ResearchHandoff の型・積み替え規則・投影関数が、上記の不変条件を満たす形で存在する。
- Run 完了と handoff 確定が同一トランザクションで成立し、停止・競合では書かれない。
- planner が handoff を受け取り、回答工程は受け取らない。
- 整理が台帳を書き換えられない形で分離されている。
- `question_context` と `AnswerBrief` への参照が production code から消えている。
