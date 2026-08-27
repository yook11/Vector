# Research Handoff Spec

Status: Draft
Updated: 2026-08-27
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

記録層と判断層を分け、層ごとに生成手段と失敗時の挙動を変える。

| 層 | フィールド | 中身 | 生成 |
|---|---|---|---|
| 判断 | `standing_inquiry` | このスレッドで継続して追っている問い | LLM が毎回書き直す |
| 記録 | `runs` | Run ごとの調査記録 | 既存成果物からコピー |
| 判断 | `next_directives` | 次のリサーチで取るべき方針 | LLM が毎回書き直す |

```python
class ResearchRunRecord(BaseModel):   # 1 Run 分の記録
    schema_version: Literal[1] = 1
    as_of: AwareDatetime
    tasks: tuple[ResearchTaskRecord, ...]                # min 1, max RESEARCH_TASK_LIMIT
    unresolved_after_search: tuple[_UnresolvedItem, ...]


class ResearchHandoff(BaseModel):
    schema_version: Literal[1] = 1
    updated_at: AwareDatetime
    standing_inquiry: str = ""
    runs: tuple[ResearchRunRecord, ...]                  # 古い順、min 1、上限なし
    next_directives: tuple[str, ...] = ()
```

記録層は Run 単位のまとまりを保つ。平坦に積むと調査時点を記録ごとに持てず、
「実行済み query は鮮度の再確認が目的の場合を除き繰り返さない」という planner の
規則が判断できなくなる。未確認がどの調査で残ったかも失われる。上限が無く積み上げ
続けるほど、この区別は重要になる。

要素型には既存の `ResearchTaskRecord`(`research_goal` / `executed_queries` /
`adopted_claims`)をそのまま使う。claim と query の紐付けは収集・精査のどこにも
残っていないため、query 軸の記録は現行の成果物からは復元できない。
「検索したが得られなかった」は `adopted_claims` が空という既存の意味づけで表す。

ユーザーの要望のうち、調査の向きに関わるもの(次はこの観点も見てほしい 等)は
`next_directives` に畳む。回答の書き方に関わる要望は今回扱わない。

### 上限

積み上げた合計には上限を置かない。コンテキスト管理が概念として固まっていないため、
どこで切るべきかを今は決められない。記録層は search Run ごとに単純追記する。

残る上限は 2 つで、いずれも既存の正本に従う。

- 要素ごとの文字数: `research_goal` 200 字、query 200 字、claim 300 字、unresolved 200 字
- 1 Run 分の記録: task 3 件、claim 合計 15 件(`ResearchRunRecord` の validator)

トレードオフとして、thread が長いほど planner prompt が伸びる。2026-08-27 の実測
(handoff 無しの planner input は 114 字):

| 積み上げ | 現実的 | 最悪 |
|---|---|---|
| 1 Run 分 | 2,169 | 6,318 |
| 3 Run 分 | 6,069 | 18,516 |
| 5 Run 分 | 9,969 | 30,714 |
| 10 Run 分 | 19,719 | 61,209 |
| 20 Run 分 | 39,299 | 122,319 |

3 Run 分は checkpoint 3 件を並べていた置き換え前と同値で、そこから先が上限を
外したぶんの増分になる。

## 生成タイミング(案)

建て直すのは調査を行った Run(`search` plan)だけとする。`direct_answer` Run は
handoff を触らない。

- answering 工程に入る時点で判断層の生成を並行起動する。この時点で plan・実行 query・
  採用 evidence・reviewer missing は揃っており、回答本文を待つ理由がない。
- Run 末尾で結果を待ち合わせ、`complete_run()` と同一トランザクションで確定する。
  attempt_epoch の競合で Run 完了が成立しなければ handoff も書かれない。
- 停止(`AnswerGenerationStopped`)では並行タスクを cancel し、何も書かない。
- 判断層の生成が失敗した場合は、記録層だけ更新し、判断層は前回の値を据え置く案とする。

判断層への入力は、前回の `standing_inquiry` / `next_directives`、今回の記録層、直近履歴を
想定する。記録層は入力として見せるだけで、書き換えさせない。

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

## Prompt 投影(案)

planner 向けの投影関数 1 本だけを置き、prompt 側は差し込むだけにする。

```python
def render_planning_instruction(handoff: ResearchHandoff | None) -> str:
    """planner prompt へ差し込む文脈。空の handoff では空文字を返す。"""
```

現在の `planning/prompts.py` の prior research render はここへ移る想定。
回答工程向けの投影関数は作らない。

## 不変条件(案)

- ResearchHandoff は調査計画の文脈であり、回答の事実根拠ではない。引用は今回の Run の
  evidence からのみ出る。投影関数を planner 向け 1 本に限ることで構造的に保つ。
- 記録層の値は上流で正規化済みの値をそのままコピーし、切り詰め・再正規化を行わない。
  上限違反は Pydantic validation が拒否する。
- 判断層の生成失敗は Run を失敗させない。
- 読み出した JSONB は投影前に必ず検証し、無効なら空として扱う。

## 既存工程への影響(案)

- `question_context` package と `AnswerBrief` を廃止する。planner と answerer は生の質問を受け取る。
- 指示語解決のため、直近履歴を要約せず planner の入力に置く。
- `research_checkpoint` package は ResearchHandoff の記録層へ吸収する。
- `AnswerProgressStage` から `context_resolution` を落とす(ステップ 5)。判断層の生成は
  回答確定前の後処理であり、進捗 stage としては見せない。

## 実装ステップ(案)

`AnswerBrief` の `active_goal` と handoff の `standing_inquiry` が同じ planner prompt に
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
3. 判断層(`standing_inquiry` / `next_directives`)を追加。並行起動と、`complete_run()` と
   同一トランザクションでの確定、失敗時の据え置き。
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
- `direct_answer` Run で handoff を建て直すこと。
- checkpoint の履歴を Run 単位で保持し続けること。

## 未決事項

- 積み上げた記録層をどこで切るか。コンテキスト管理の概念が固まってから決める。

## Done の目安

- ResearchHandoff の型・積み替え規則・投影関数が、上記の不変条件を満たす形で存在する。
- Run 完了と handoff 確定が同一トランザクションで成立し、停止・競合では書かれない。
- planner が handoff を受け取り、回答工程は受け取らない。
- `question_context` と `AnswerBrief` への参照が production code から消えている。
