# Agent 会話文脈 resolution slice 仕様 (Phase 2 Slice 1)

## 位置付け

親仕様: `specs/agent/conversation-history-async-runs.md`（repo root 相対。repo 直下
`specs/` は gitignored なローカル正本であり、git 追跡文書からは参照が dangling に
見える点に注意）。前提 slice: Slice 1〜5
（会話履歴 schema / 非同期 run / thread UI / progress_stage / Redis ライブイベント
すべて実装済み）。本 slice は親仕様で「Phase 2（Ask First）」と先送りされていた
**過去履歴を agent の入力文脈に使う**機能の最初の slice を実装する。

Ask First は消化済み（2026-07-10 ユーザー合意）。合意内容:

- run 冒頭に **resolution（condense）ステップを 1 段追加**し、履歴の解釈を
  そこへ集約する。下流の検索機構は変えない。
- resolution の出力は質問文 1 本でなく **4 フィールドの構造化オブジェクト**
  （`standalone_question` / `user_intent` / `prior_coverage` /
  `user_activity_context`）。`prior_coverage`（既回答の要約 = 負の制約）と
  `user_activity_context`（作業の流れ = 正の方向付け）は planner / synthesizer
  での効き方が違うため分離する。
- **direct 経路には要約でなく直近 assistant 回答の本文 verbatim を渡す**
  （「表にして」「前回の結論だけ」は前回回答の整形であり、要約からの再生成では
  数値・結論が静かに変わり得るため）。
- 文脈は **thread-scoped のみ**。cross-thread の長期メモリは別設計（Non-goals）。

## 親仕様の改訂（本 slice で明示的に行う）

1. `conversation-history-async-runs.md:39` の Non-goal
   「過去履歴を agent の入力文脈に使う（Phase 2 …）」を削除し、後続表の
   「Phase 2 会話文脈」を本 slice 参照に差し替える。
2. Slice 5 で確立した Redis イベントの禁止事項
   「元質問の本文・回答断片は入れない。LLM 生成済み検索クエリ（external のみ）は
   含めてよい」を次に改訂する:

   > 元質問の本文・回答断片は入れない。**LLM 生成物**（external 検索クエリ、
   > resolution が生成した standalone_question）は含めてよい。

   根拠は Slice 5 の改訂と同じ: recentEvents は run 所有者にのみ返り、
   standalone_question は `_clean` 相当の検証（文字数 cap）を通過した LLM 生成物に
   限る。発火条件は **LLM 成功 かつ standalone_question（strip 後）≠ 元質問
   （strip 後）** の両方とする。skip / fallback は値が元質問 verbatim のため
   発火せず、LLM 成功でも prompt 規約（自己完結なら元質問をほぼそのまま返す）に
   よる echo は同値比較で落とす。これで元質問 verbatim の Redis 混入を構造的に
   排除する。

## Problem

follow-up 質問（「もっと詳しく」「それの株価への影響は？」「表にして」）で、
ユーザーが何を求めているかが agent に一切伝わらない。現状は
`AnswerQuestionInput.question`（元質問 verbatim）1 本が全段の入力で:

- planner は代名詞・省略を解決できず、既に回答した内容と同じ goal を立てる。
- planner 失敗時の fallback は元質問 verbatim を `internal_queries` に入れるため
  （`planning/service.py:257` → `safe_fallback_plan`）、「それの影響は？」で
  ベクトル検索する無意味な run になる。
- direct 経路（`planned_mode="none"`。「もっと短く」「前回の結論だけ」等の
  言い換え要求が振られる）は `question` しか受けず
  （`answering/direct.py:66`）、前回回答に触れられない。
- synthesizer も同様に文脈を知らず、前回と同じ内容を繰り返す。
- ユーザーからは agent が follow-up をどう解釈したか見えない。

## Evidence（調査済みの既存構造）

- **worker の質問取得**: `run_agent_answer`（`queue/tasks/agent_run.py:41`）は
  `acquire_for_execution`（`runs/repository.py`）で
  `AgentRun.user_message_id` join 経由の `agent_messages.content` を読み、
  `PreparedAgentRun(run_id, thread_id, question)` を得て
  `AnswerQuestionInput(question=..., as_of=now)` を構築する（`agent_run.py:69-72`）。
  **thread の過去メッセージはどこからも読んでいない。**
- **履歴の読み口**: `agent_messages` は `(thread_id, seq)` UNIQUE・`role`
  （'user'/'assistant' CHECK）・`content`（Text 非空 CHECK）を持つ
  （`models/agent_message.py:36`）。直近 N 件を取る repository 関数は無い
  （`read_thread_detail_for_user` は全件・表示用）。失敗 run は assistant
  メッセージを持たない（`assistant_message_id` nullable）ため、履歴には
  成功回答と user 質問だけが自然に残る。
- **配布先のシグネチャ**（すべて keyword-only で拡張余地あり）:
  - planner: `QuestionPlanner.plan(input: AnswerQuestionInput)`
    （`planning/planner.py:13`）。`fallback_query=input.question` は
    `planning/service.py:120,143,257` の 3 箇所。
  - synthesizer: `EvidenceAnswerSynthesizer.synthesize(*, question, evidence,
    as_of, target_time_window)`（`answering/synthesis.py:110-120`）。
  - direct: `DirectAnswerer.answer(*, question, as_of)`
    （`answering/direct.py:66-71`）。呼び出しは
    `QuestionAnsweringService.answer` の `NoRetrievalPlan` 分岐
    （`answering/service.py:76-79`）。
- **AnswerQuestionInput は frozen の 2 フィールド**（`agent/contract.py:52-58`）。
  デフォルト付きフィールド追加は既存呼び出しに非破壊。
- **ライブイベント基盤は Slice 5 で完備**: publisher は worker が
  `agent_run.py:52` で生成済み（resolution の発火点から到達可能）。
  `ResearchRunEvent` union（`schemas/research.py:99-106`）は前方互換規約
  （frontend は未知 type を無視）で語彙追加が non-breaking。
- **progress_stage の CHECK は 3 値固定**（'planning'/'retrieving'/'synthesizing'、
  `models/agent_run.py`）。stage 追加は migration になるため本 slice では
  行わない。resolution 中の表示は `running × progressStage=null` の既存
  default 文言「生成中」（`ActiveRunStatus.tsx:29-42`）で許容。
- **AI generator の配線パターン**: `GeminiQuestionPlanner()` 等を
  `composition.py:59-77` で直接生成し Protocol 実装として注入。settings 経由の
  lazy API key チェック（`planning/ai/gemini.py:48-52`）。resolution も同型。

## 設計判断

1. **新 module `app/agent/question_resolution/` を新設し、resolution は
   agent core の外（worker 側）で実行する。**
   resolution は thread messages（DB）を読む必要があるが、agent core は DB を
   知らない境界を維持する（Slice 5 と同じ規律）。worker が履歴をロードし、
   resolver を呼び、**enriched な `AnswerQuestionInput` を構築して**既存の
   `agent.answer()` に渡す。agent core から見える変化は入力フィールドの増加のみ。

2. **contract: `ResolvedQuestion`（frozen、文字数 cap は構造的に強制）**

   ```python
   class ResolvedQuestion(BaseModel):
       model_config = ConfigDict(frozen=True)

       standalone_question: str   # 非空・max 500 字。retrieval / fallback の正本
       user_intent: str = ""            # max 500 字。今回どう答えてほしいか
       prior_coverage: str = ""         # max 1500 字。会話で既に回答済みの内容の要約
       user_activity_context: str = ""  # max 1000 字。ユーザーの作業・調査の流れ
   ```

   - `standalone_question` 以外は**空文字可**。履歴が薄い時（2 通目等）に
     LLM へ埋めることを強制すると、もっともらしい「流れ」を捏造して planner の
     goal を静かに歪めるため、「無ければ空」を prompt とスキーマの両方で許す。
   - LLM 出力は draft（`ResolvedQuestionDraft`）→ cleaning
     （strip + 超過 truncate、`_clean_generated_queries` と同型）→
     `ResolvedQuestion` 構築の順で検証する。cap は draft 側で丸め、
     `ResolvedQuestion` の max_length は最終ガード。

3. **service: `QuestionResolutionService.resolve(*, question, history, as_of)
   -> ResolvedQuestion`**（`QuestionResolver` Protocol の実装）。

   - **skip**: `history` が空（thread の初回メッセージ）なら LLM を呼ばず
     `ResolvedQuestion(standalone_question=元質問, 他は空)` を返す。
   - **prompt 規約**: 質問が自己完結なら standalone_question は元質問を
     ほぼそのまま返す / 代名詞・省略は履歴から解決する / 各フィールドは
     根拠が履歴に無ければ空にする。
   - **fallback**: LLM 失敗（`AIProviderError` / 出力不正）時は skip と同じ値で
     続行し、run を落とさない（現状と同一の劣化。warning log は run_id +
     失敗分類のみで質問本文を載せない）。
   - **metrics**: `record_question_resolution_outcome(result=
     "resolved" | "skipped" | "failed")` を service（決定境界の所有者）が emit。
   - generator は `question_resolution/ai/gemini.py` に
     `GeminiQuestionResolver`（planner と同じ配線パターン・軽量モデル）。

4. **履歴窓は repository の新メソッドで読む**:
   `AgentThreadRepository.read_recent_messages_before(*, thread_id, before_seq,
   limit) -> list[ThreadMessageSnapshot]`（`seq < before_seq` を seq 降順で
   limit 件 → 昇順に反転して返す。snapshot は role + content のみ）。

   - `PreparedAgentRun` に user message の `seq` を追加する
     （acquire の既存 select に `AgentMessage.seq` を足すだけ）。
   - 窓の定数（`question_resolution/service.py`）:
     `HISTORY_MESSAGE_LIMIT = 6`（約 3 往復）/
     `HISTORY_MESSAGE_CHAR_CAP = 2000`（prompt 投入時に 1 メッセージを先頭
     2000 字で切る。sources / evidence は履歴に含めない）。
   - `previous_answer`（direct 用 verbatim）は**同じ窓の最新 assistant
     メッセージの content**（無ければ空）。resolution とは別に worker が
     窓から直接取り出す（LLM を経由させない = 改変されない）。cap しない
     （回答長は synthesis 側の生成で既に有界。要約でなく原文であることが要件）。

5. **`AnswerQuestionInput` の拡張（非破壊）**:

   ```python
   class AnswerQuestionInput(BaseModel):
       question: str                     # = resolved.standalone_question
       as_of: datetime
       user_intent: str = ""
       prior_coverage: str = ""
       user_activity_context: str = ""
       previous_answer: str = ""         # 直近 assistant 回答 verbatim（direct 専用）
   ```

   `question` に standalone_question を流すことで、**planner の
   fallback_query（3 箇所とも `input.question`）と retrieval の正本が自動的に
   解決済みの質問になる**。fallback 経路の「それ」問題はこの代入だけで消える。
   `planning/service.py` の変更は不要。

6. **各段への配布**（`QuestionAnsweringService` が input から渡す）:

   | 段 | 受け取るもの | 使い方 |
   |---|---|---|
   | planner | input 丸ごと（4 フィールド） | prompt に intent / coverage / activity を追加。既出を避け、流れに沿った goal |
   | retrieval / fallback | `input.question` のみ | **機構不変**。context フィールドは検索クエリ・goal に verbatim 混入させない |
   | synthesizer | `question` + `user_intent` + `prior_coverage` + `user_activity_context`（kwargs 追加、default ""） | 回答の**形**を決めるだけ。事実根拠は evidence のみ（既存の接地 validator 不変） |
   | direct_answerer | `question` + `user_intent` + `user_activity_context` + `previous_answer`（kwargs 追加、default ""/None 相当） | 言い換え・整形は previous_answer の原文に対して行う |

   direct に `prior_coverage` は渡さない（previous_answer 原文が上位互換）。
   契約上の整理: **言い換え回答（direct）は sources なしのままで許容し、
   direct の出力 content は引用 marker `[[N]]` を含まない**。previous_answer には
   前回回答の marker が含まれ得るが、新メッセージは sources=[] のため残存 marker は
   UI で silent に除去され（`CitedAnswerContent.tsx:45` の miss 時 drop）、DB には
   ゴミ marker が永続化される。これを防ぐため prompt 指示に頼らず、direct 出力への
   決定的な post-process（`[[N]]` 除去）で構造的に保証する。出典は前回 assistant
   メッセージ側に永続化されており、`planned_mode="none" → sources 禁止` の
   validator（`agent/contract.py:178`）は触らない。

7. **解釈の見える化: ライブイベント `question.resolved` を追加する。**

   | event type | 属性 | 発火点 |
   |---|---|---|
   | `question.resolved` | `standalone_question: str` | worker: resolve が **LLM 成功**で返り、かつ **standalone_question（strip 後）≠ 元質問（strip 後）** の場合のみ |

   - 発火は worker（`run_agent_answer`）が既存 publisher で行う。skip / fallback
     時に加え、LLM 成功でも元質問と同値（echo）の場合は発火しない
     （verbatim 混入の排除と、解釈が起きていない時に表示する価値がないことが
     同じ方向を向く。親仕様の改訂参照）。
   - frontend `ActiveRunStatus`: `running × progressStage ∈ {null, "planning"}`
     の間、`question.resolved` があれば「“{standaloneQuestion}”について調査中」を
     サブテキスト表示。retrieving 以降は既存の検索イベント表示が優先
     （表示条件が排他なので既存ロジックに分岐を 1 つ足すだけ）。
   - DB への永続化はしない（Slice 4/5 の分類どおり: 復元価値が要ると
     判断したら別 slice で Ask First。thread 詳細の表示は従来のまま）。

8. **prompt 安全境界: 新規に prompt へ入る文は全投入先で既存の untrusted 境界を
   必須とする。** 機構は既存 prompt と同一
   （`app/analysis/prompt_safety.py` の `sanitize_for_untrusted_block` +
   role 別 `<untrusted_input>` ブロック。planner / synthesizer / direct /
   external_search の現行 prompt が同型）。対象は次の 3 系統:

   - resolution prompt に入る**履歴メッセージ**（user / assistant content）。
   - direct prompt に入る **previous_answer**（境界で包む。sanitize は境界タグの
     エスケープに限られ、整形対象としての原文性は保たれる）。
   - 下流（planner / synthesizer / direct）prompt に入る **resolved 4 フィールド**。
     LLM 生成物だが、履歴由来の指示文が resolver を通過して残存し得るため、
     元質問と同じく untrusted として扱う。

## API Contract（/api-contract + /gen-types）

REST の endpoint / request / response 形状は不変。`ResearchRunEvent` union に
1 メンバー追加のみ（前方互換規約の範囲内）:

```text
ResearchRunEvent に追加:
  { type: "question.resolved", ts, standaloneQuestion: string }
```

## 実行フロー

```text
worker run_agent_answer:
  acquire（PreparedAgentRun: run_id, thread_id, question, user_message_seq）
  → publisher 生成 + reset()（従来どおり）
  → history = repository.read_recent_messages_before(
        thread_id, before_seq=user_message_seq, limit=6)
  → resolved = QuestionResolutionService.resolve(
        question=prepared.question, history=history, as_of=now)
      - history 空 → skip（LLM 呼ばず passthrough）
      - LLM 失敗 → fallback（passthrough + warning + metric）
      - 成功かつ standalone_question ≠ 元質問（strip 後比較）
        → publisher.event_occurred(question.resolved)
  → previous_answer = history 内の最新 assistant content（無ければ ""）
  → input = AnswerQuestionInput(
        question=resolved.standalone_question, as_of=now,
        user_intent=..., prior_coverage=..., user_activity_context=...,
        previous_answer=previous_answer)
  → agent.answer(input)  # 以降は既存フロー
      planner: prompt に context 3 フィールド追加（fallback_query は自動で解決済み）
      NoRetrievalPlan → direct.answer(question, as_of,
                          user_intent, user_activity_context, previous_answer)
      retrieval 系 → collect(plan)（不変）
                   → synthesizer.synthesize(question, evidence, as_of,
                          target_time_window, user_intent, prior_coverage,
                          user_activity_context)
  → complete_run / fail 経路は不変
```

## New Types / Structure

```text
backend/app/agent/question_resolution/
  __init__.py
  contract.py       (ResolvedQuestion / ResolvedQuestionDraft / QuestionResolver
                     Protocol / cleaning / cap 定数)
  service.py        (QuestionResolutionService: skip / fallback / metrics、窓定数)
  metrics.py        (record_question_resolution_outcome)
  ai/gemini.py      (GeminiQuestionResolver: 履歴+質問 → draft)
backend/app/agent/contract.py            (AnswerQuestionInput 4 フィールド追加 +
                                          QuestionResolvedEvent を event union に追加)
backend/app/agent/threads/repository.py (read_recent_messages_before)
backend/app/agent/runs/contracts.py            (PreparedAgentRun.user_message_seq)
backend/app/agent/runs/repository.py           (acquire時にuser_message_seqを投影)
backend/app/agent/answering/service.py   (synthesizer / direct への配布)
backend/app/agent/answering/direct.py    (DirectAnswerer / Generator の kwargs 拡張 +
                                          prompt: previous_answer の整形指示 +
                                          出力の [[N]] marker 除去 post-process)
backend/app/agent/answering/synthesis.py (Synthesizer / Generator の kwargs 拡張 +
                                          prompt: 形は context、事実は evidence)
backend/app/agent/planning/ai/gemini.py  (prompt に context 3 フィールド追加)
backend/app/agent/composition.py         (resolver は含めない — worker 所有。
                                          build_question_resolver() を追加提供)
backend/app/queue/tasks/agent_run.py     (履歴ロード → resolve → event 発火 →
                                          enriched input 構築)
backend/app/schemas/research.py          (ResearchRunQuestionResolvedEvent 追加)
frontend/src/features/research/components/
  ActiveRunStatus.tsx                    (planning 中の resolved サブテキスト)
frontend/src/types/*.gen.ts              (/gen-types 再生成)
```

**DB 変更なし・migration 不要**（`agent_messages` から読むだけ。progress_stage
の語彙も増やさない）。

## Invariants

- agent core（contract / planning / answering）は DB・Redis・履歴ロードを
  知らない。文脈は enriched `AnswerQuestionInput` としてのみ流入する。
- retrieval / fallback が消費するのは `input.question`（= standalone_question）
  のみ。`user_intent` / `prior_coverage` / `user_activity_context` /
  `previous_answer` を検索クエリ・collection goal・fallback に verbatim
  混入させない。
- resolution の失敗で run を落とさない。劣化は「元質問 + 空 context で続行」
  （= 現状の挙動）のみ。
- resolution の log / metric に質問本文・回答断片・resolved フィールドの内容を
  載せない（run_id + outcome / 失敗分類のみ。Slice 4/5 と同じ規律）。
- `question.resolved` イベントは resolution LLM が成功し、かつ
  standalone_question（strip 後）が元質問（strip 後）と異なる場合のみ発火する
  （skip / fallback / echo いずれの経路でも元質問 verbatim を Redis に載せない）。
- 新規に prompt へ入る文（履歴メッセージ / previous_answer / resolved
  4 フィールド）は、全投入先で `sanitize_for_untrusted_block` + role 別
  `<untrusted_input>` 境界を通す（既存 prompt と同じ規律）。
- direct の出力 content は引用 marker `[[N]]` を含まない（post-process で
  構造的に除去。sources を持たない契約と整合し、DB にゴミ marker を残さない）。
- `standalone_question` 以外の resolved フィールドは空文字を正当な値とする
  （LLM に捏造を強制しない）。
- 事実の接地は evidence のみ: synthesizer への context は回答の形（深掘り /
  差分 / 構成）を決める用途に限り、`answered × sources` の既存 provenance
  validator は不変。
- direct の `previous_answer` は同 thread の表示済み assistant メッセージの
  verbatim であり、新しい情報露出を作らない。言い換え回答が sources を
  持たない契約（`planned_mode="none"` → sources 禁止）は維持する。
- resolution は thread-scoped: 読む履歴は当該 thread の `seq < 当該 user
  message` のメッセージのみ。他 thread・他 user のデータに触れない。

## Non-goals

- cross-thread の長期メモリ（保存・表示・削除・ユーザー制御を伴う別設計）。
- `user_intent` の enum 化（taxonomy の先行固定はしない。自由文で開始）。
- resolved question の DB 永続化・thread 詳細への表示（復元価値が要るなら
  別 slice で Ask First）。
- progress_stage への "resolving" 追加（CHECK 変更 = migration。既存の
  「生成中」default 表示で許容）。
- synthesizer への前回回答 verbatim 注入（まず prior_coverage 要約で開始し、
  品質を実測してから判断）。
- 履歴窓の動的調整・要約のキャッシュ・トークン数ベースの budget 管理。

## Tests

backend:

1. contract: cleaning（strip / 超過 truncate / 空フィールド許容）/
   standalone_question 非空必須 / cap 超過入力が draft cleaning で丸まる。
2. resolution service（stub generator）: history 空で LLM を呼ばず passthrough
   （skipped metric）/ LLM 失敗で passthrough + warning に質問本文が無い
   （capture_logs）+ failed metric / 成功で 4 フィールドが流れる
   （resolved metric）。
3. repository: `read_recent_messages_before` が seq 昇順・最大 limit 件・
   `before_seq` 以降（当該 user message 含む）を含まない / 他 thread の
   メッセージが混入しない。
4. 配布（stub planner / synthesizer / direct）: planner が context 付き input を
   受ける / retrieval 系 plan で synthesizer に intent + coverage +
   activity_context が渡る / NoRetrievalPlan で direct に intent +
   activity_context + previous_answer が渡り prior_coverage は渡らない /
   context が空でも全段が従来どおり動く（初回互換）。
5. fallback 救済: planner 失敗時の `safe_fallback_plan` の internal_queries が
   standalone_question になっている（enriched input 経由の end-to-end）。
6. worker 統合: resolve 成功かつ非 echo 時のみ `question.resolved` が publish
   される / skip・fallback・echo（standalone_question == 元質問、strip 後比較）時は
   publish されない / previous_answer が窓の最新 assistant content から取られる
   （assistant 不在なら空）。
7. prompt 安全境界: 履歴 / previous_answer / resolved フィールドに境界タグ
   （`</untrusted_input>` 等）を含む文字列を入れても、各 prompt 組み立て
   （resolution / planner / synthesizer / direct）で sanitize され境界脱出しない。
8. direct marker 除去: generator 出力（stub）に `[[1]]` を含めても DirectAnswerer
   の返す content に marker が残らない（previous_answer 由来の marker 再現ケース）。
9. API / schema: `question.resolved` が `ResearchRunEvent` union に載り OpenAPI に
   出る（/gen-types）。

frontend:

10. ActiveRunStatus: `running × progressStage null/planning` で
   「“…”について調査中」が表示される / retrieving 移行で検索イベント表示に
   切り替わる / `question.resolved` 不在なら従来の stage 文言のみ /
   既存テスト green のまま。

## 検証の制約

- dev の docker backend は外向きネットワーク無しのため、resolution LLM の
  実観測は不可。unit（stub / fake generator）中心で、dev では skip 経路
  （初回メッセージ）と fallback 経路（provider 不設定）を確認する。
  実際の書き直し品質・prior_coverage / user_activity_context の充足具合の
  実測は本番 deploy 後の follow-up とする。
- resolution 追加による run 全体のレイテンシ増（LLM 1 呼び出し、数秒級）は
  非同期 run + progress 表示の枠内で許容。実測は本番で行う。
- `/check` + `/gen-types`。`/migration` 不要。

## Done

- follow-up 質問（代名詞・省略・「もっと詳しく」）で、planner の goal と
  fallback query が解決済みの standalone_question に基づいて立つ。
- 「表にして」「前回の結論だけ」系の direct 経路が前回回答の原文を受けて
  整形できる。
- 実行中の UI に「“…”について調査中」の解釈が live 表示される
  （skip / fallback / echo 時は従来表示のまま）。
- direct の出力 content に引用 marker が残らず、新規 prompt 投入文の全箇所が
  untrusted 境界を通っている（テスト 7・8 で検証済み）。
- 初回メッセージの run は LLM 追加呼び出しなしで従来と同一の挙動。
- resolution の失敗が run を落とさず、log に質問本文が載らない。
- 親仕様の Phase 2 行と Slice 5 の Redis イベント禁止事項が改訂されている。
- 新規テスト green + 既存 suite green + /gen-types 済み。
