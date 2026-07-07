# 並列 retrieval 実装 slice 仕様 (Slice C-1)

## 位置付け

Q&A エージェントのコア工程 (planning / retrieval / evidence / 4E / 4D) は
実装済み。API 公開 (C-2) の前に、retrieval 工程内の直列待ちを解消する。

| slice | 内容 | 状態 |
|---|---|---|
| C-1 | 並列 retrieval (本 slice) | **本 slice** |
| C-2 | API endpoint (`POST /api/v1/research/responses`) | 仕様固定済み・未着手 |
| C-3 | インライン引用 (`[[N]]` marker) | 仕様固定済み・未着手 |

## Problem

- `InternalAndExternalPlan` の実行が internal → external の直列
  (`retrieval.py` の `InternalAndExternalPlan` 分岐)。internal 検索
  (embedding + pgvector) の完了を external 検索が待つ意味がない。
- external 検索の内部は既に並列 (`external_search/runner.py` の
  `Semaphore + asyncio.gather`)。直列の継ぎ目はこの 1 箇所だけ。
- 短縮幅は internal 検索の所要時間ぶん (external が支配的)。同期 API
  (C-2) のレイテンシ成立性を上げる。

## Evidence

- `backend/app/agent/answering/retrieval.py` —
  `QuestionPlanRetrievalService.retrieve` の match dispatch。
  `InternalAndExternalPlan` 分岐で internal を await してから external を
  開始している。
- `backend/app/agent/external_search/runner.py:67-80` — external 内部の
  並列実行の前例 (`asyncio.Semaphore` + `asyncio.gather`)。
- `backend/app/agent/internal_retrieval/service.py:136-152` — internal
  検索の途中 side effect: query embedding cache の store (operation ごと
  独自 session で DB 書込) と cache outcome metrics。キャンセルすると
  これらが飛ぶ (並列化の設計判断 4 の根拠)。
- `backend/tests/agent/answering/test_retrieval.py` — 既存の dispatch /
  unmet 意味論のテスト。

## 合意済みの設計判断

1. **並列化は `InternalAndExternalPlan` 分岐のみ**。internal 単独 /
   external 単独の経路は変更しない (並列にする相手がいない)。
2. **API リクエスト自体は同期のまま** (C-2 で確定済み)。本 slice は
   リクエスト内部の並列化であり、job 投入型 (202 + polling) ではない。
3. **失敗の意味論を保存する**。並列化は速度の変更であって、失敗時の
   観測可能な振る舞いを変えない。**「観測可能な振る舞い」には途中
   side effect (internal の query embedding cache store / metrics) を
   含める**。
4. **片側失敗でもキャンセルせず、両方を回収してから例外を選ぶ**
   (`asyncio.gather(return_exceptions=True)` 相当)。first-exception
   cancel は採らない:
   - キャンセルすると internal の cache store / metrics が飛び、直列時
     (external 失敗時点で internal は完了済み) と振る舞いが変わる。
   - 回収方式ならキャンセル・`ExceptionGroup` unwrap・タスクリークの
     手当てが不要で、実装も決定的になる。
5. **例外の優先順位は internal 先** (両方失敗した場合)。直列時は
   internal が先に走るため、internal の例外が伝播するのが意味論の保存。
   timing 依存にしない。
6. **受容する逸脱 (明文化)**: internal が失敗しても external は完走する。
   直列なら external は開始されず外部 API コストもゼロだったが、この
   経路に来る例外は想定外系のみ (external service は provider 失敗を
   outcome に吸収済み、internal の例外は DB / embedding の想定外) で
   稀なため、稀な失敗経路での外部 API コスト・レイテンシを受容する。
   非対称キャンセルによる節約は timing 依存の複雑さに見合わない。

## Invariants

- 片方が例外でも両方のタスクを必ず回収する (リークタスクなし)。
  例外は**元の型のまま**伝播し、両方失敗時は internal の例外を優先する。
- 完走した側の side effect (cache store / metrics) は直列時と同様に
  発生する (途中キャンセルで飛ばさない)。
- 結果の合成は直列時と同一: `RetrievalOutcome` の内容 (internal_hits /
  external_search / unmet_requirements) と意味論を変えない。
  external searcher 未配線 (None) → `unmet_requirements=["external_search"]`
  も既存どおり。
- port signature (`InternalArticleRetriever` / `ExternalPlanSearcher`) と
  `RetrievalOutcome` 契約は変更しない。
- 並列度の新しい設定・上限は追加しない (同時実行は internal / external の
  2 本だけ)。

## Non-goals

- API endpoint / DI (C-2)。
- external 検索内部の並列度チューニング (既に並列、runner の責務)。
- internal 検索自体の高速化。
- リクエスト外への非同期化 (job / polling / SSE)。

## Changed Files

```text
backend/app/agent/answering/retrieval.py       (InternalAndExternalPlan 分岐)
backend/tests/agent/answering/test_retrieval.py (並列・失敗意味論テスト追加)
```

## Tests

fake retriever に `asyncio.Event` を仕込み、実時間 sleep に依存せず検証する。

1. **重なりの正本テスト**: internal / external の両方が開始してから
   どちらかが完了する (両 fake が「相手の開始」を待ってから返る形にし、
   直列実装では永久待ちになる構造にする)。**必ず短い
   `asyncio.wait_for` timeout で包む** — timeout なしだと直列実装で
   fail ではなく CI ハングになる。
2. 結果の合成が直列時と同一: internal_hits + external_search が両方
   詰まった `RetrievalOutcome` が返る。
3. internal が例外 → external は**キャンセルされず完走し** (fake の
   完了フラグで確認)、internal の例外が**元の型のまま**伝播する。
4. external が例外 → internal は完走し (side effect 相当の完了フラグで
   確認)、external の例外が元の型のまま伝播する。
5. **両方が例外 → internal の例外が優先して伝播する** (優先順位の
   正本テスト)。
6. external searcher 未配線 (None) + internal 成功 →
   `unmet_requirements=["external_search"]` + internal_hits (既存意味論の
   保存、既存テストがあれば流用)。
7. internal 単独 / external 単独プランの既存テストが無変更で green
   (経路を触っていない確認)。

## Done

- `InternalAndExternalPlan` で internal / external が同時起動する
  (テスト 1 が green)。
- 失敗時の例外型・優先順位 (internal 先)・side effect 保存・結果合成・
  unmet 意味論が仕様どおり。
- 既存 suite に regression なし。実 API・probe は不要 (テストのみで検証)。
