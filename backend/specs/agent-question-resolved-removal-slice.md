# Agent 質問言い換え live 通知削除 slice 仕様

更新日: 2026-08-19

実装状況: Implemented

## 位置付け

`AnswerBrief.standalone_question` は後段モデルへの内部の伝え方である。
履歴つきで言い換わったときだけ `context_resolution.question_resolved` を出し、
UI が「“…”について調査中」と見せていた口を外す。

前提: `backend/specs/agent-answer-brief-slice.md`。
`RunHooks` は当該通知のためだけに存在した。

## Work Definition

### Problem

- コンテキスト整理の内部フィールドを、ユーザー向けの言い換え結果として live 通知している。
- `RunHooks` / `QuestionResolvedRunHooks` はその通知のための継ぎ目で、他の観測を持たない。

### Evidence

- 具象 hook は `running/hooks.py` の `QuestionResolvedRunHooks` だけ。
- Runner は Brief 準備直後に `hooks.on_answer_brief_prepared(...)` を呼ぶ。
- worker は `hooks=QuestionResolvedRunHooks(events=activity_reporter)` を渡す。
- 公開契約は `QuestionResolvedEvent` と `ResearchRunQuestionResolvedEvent`。
- UI は `ActiveRunStatus` が `“{standaloneQuestion}”について調査中` を出す。

### Invariants

- `AnswerBrief` と progress stage `context_resolution` は残す。
- `AnswerProgressEvent` / `ResearchRunEvent` に `context_resolution.question_resolved` は残らない。
- `RunHooks` / `QuestionResolvedRunHooks` / `hooks.py` は残さない。空 Protocol も置かない。
- `AnsweringRunner.run()` は `hooks=` を持たない。prepare の次は phases factory。
- Redis / SSE に残った旧 payload は decode miss / `unknown_activity` として捨て、GET は 500 にしない。

### Non-goals

- `context_resolution` stage の改名・削除。
- AnswerBrief / Question Context Agent / prompt の変更。
- 検索・精査 activity の表示変更。
- DB / migration、歴史仕様の一括置換。
- 消した概念専用テストを「出さない」へ反転して残すこと。

### Done

- 公開 live 契約から当該 variant が消える。
- Runner に hook 継ぎ目がなく、worker は `activity_reporter` を検索・精査用にだけ渡す。
- planning / context_resolution 中の status は stage 文言のみ。

## 表示

- `answering`: 詳細なし
- `evidence_collection` / `evidence_review`: 最新の収集・精査 activity
- それ以外: 詳細なし

## テスト所有

- `tests/agent/running/test_hooks.py` は削除する。
- Runner / workflow の timeline から hook を外す。
- live reporter / recent events の known activity から当該 fixture を外す。
- frontend は旧 type を捨て、planning は stage のみを出す。
