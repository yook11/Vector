# 外部検索貫通 probe slice 仕様

## 位置付け

検索基盤 (external_search / internal_retrieval / answering.retrieve) は全 unit
テスト green だが、すべて mock 検証であり、実 API (DeepSeek / Tavily) に対して
plan → retrieve → evidence が通ることは一度も確認されていない。

本 slice は host 実行の probe script で、**external 経路のみ**を実環境で
貫通させ、検索結果が返ってくるところまでを確認する。planner 貫通・internal
貫通・Evidence 正規化・回答生成・API endpoint は後続 slice の責務とする。

## Problem

- composition (具象の組み立て) がどこにも存在せず、`QuestionAnsweringService`
  の external 分岐が実 adapter 込みで動くことを確認する手段がない。
- 実 Tavily / DeepSeek を通した `ExternalSearchOutcome` の形 (evidence /
  task_reports / dedup 会計) を観測したことがない。

## Evidence

- `backend/app/agent/answering/service.py`
  - `retrieve(plan, *, as_of)`。`external_search: ExternalPlanSearcher | None`
    と `requested_external_agent_count` を constructor 注入。
- `backend/app/agent/evidence_collection/external_search/service.py`
  - `ExternalSearchService(runner=...)`。`search_plan()` が agent 数 clamp と
    URL dedup を行い `ExternalSearchOutcome` を返す。
- `backend/app/agent/evidence_collection/external_search/runner.py`
  - `ExternalSearchResearchRunner(query_generator=, search_provider=,
    evidence_selector=)`。段階別 timeout (30s / 15s / 30s) 内蔵。
- `backend/app/agent/evidence_collection/external_search/tavily.py`
  - `TavilySearchProvider(api_key=SecretStr, client=TavilyHttpClient)`。
    api_key 空で ValueError。
- `backend/app/agent/evidence_collection/external_search/ai/deepseek.py`
  - `DeepSeekQueryGenerator()` / `DeepSeekEvidenceSelector()` は引数なしで
    settings から client を構築する。
- `backend/app/agent/contract.py`
  - external plan は `external_research_tasks` (collection_goal、最大 3、
    goal unique) が必須。`internal_queries` は空であること。
- `backend/app/config.py`
  - `deepseek_api_key` / `tavily_api_key` (いずれも SecretStr、default 空)。
- `backend/scripts/probe_tavily_search.py`
  - probe の既存流儀: settings 経由・`make_safe_async_client()`・未設定は
    SystemExit・raw response は保存しない。
- dev docker backend は外向きネットワークなし → probe は host 実行。

## Invariants

- 秘密情報は settings 経由でのみ扱う。`.env` を読まない・キー値を stdout /
  エラーメッセージに出さない。必要なキー未設定なら API を呼ぶ前に
  `SystemExit` で明示的に落とす。
- `app/` 配下に変更を加えない。組み立ては script 内に閉じる (正式な DI は
  API endpoint slice で行う)。
- 貫通経路は `QuestionAnsweringService.retrieve()` を通す。
  `ExternalSearchService` の直呼びにしない (answering dispatch 込みの検証が
  目的のため)。
- plan は planner を呼ばず script 内で明示構築する。external mode の
  validator (`external_research_tasks` 必須・`internal_queries` 空) を満たす。
- `internal_search` には呼ばれたら即 raise する stub を渡す (サイレント
  no-op にしない)。external mode で内部検索が呼ばれないことの検証を兼ねる。
- `as_of` は script 開始時に UTC now を 1 回生成して使い回す。
- 失敗は隠さない: 例外は握りつぶさず non-zero 終了。成功時も
  `task_reports` / `deduplicated_evidence_count` / `unmet_requirements` を
  必ず表示する。
- 成功判定は形で行う: validate 済み `ExternalSearchOutcome` が返ること・
  会計 (task / evidence / dedup 件数) が表示されること。evidence の内容
  (特定記事) には依存しない。
- default は最小コスト実行: task 1 件・`--agents 1`。
- pytest に組み込まない (実 API・課金のため手動実行のみ)。raw response は
  保存しない。

## Non-goals

- planner (Gemini) 呼び出しの貫通。
- internal 検索の貫通 (host から dev DB への接続手当てが必要。次の貫通 slice)。
- Evidence 正規化 / source_ref 採番 / 回答生成 / `answer()`。
- FastAPI endpoint / Depends 配線 / frontend 型生成。
- `app/` 配下の変更、新規 unit テストの追加。
- retry / rate limit 制御の追加 (既存 runner の timeout に委ねる)。

## New Files

```text
backend/scripts/probe_question_answering.py
```

## CLI

```text
uv run python scripts/probe_question_answering.py \
  [goal ...]                # collection_goal (最大 3)。省略時は default 1 件
  [--agents N]              # requested_agent_count。default 1
  [--time-window TEXT]      # plan.target_time_window。default なし
```

## Behavior

```text
1. settings から tavily_api_key / deepseek_api_key の設定有無を確認
   (未設定は SystemExit)
2. make_safe_async_client() の中で具象を組み立てる:
     provider = TavilySearchProvider(api_key=settings.tavily_api_key, client=client)
     runner   = ExternalSearchResearchRunner(
                  query_generator=DeepSeekQueryGenerator(),
                  search_provider=provider,
                  evidence_selector=DeepSeekEvidenceSelector())
     service  = QuestionAnsweringService(
                  internal_search=_UnreachableInternalSearch(),
                  external_search=ExternalSearchService(runner=runner),
                  requested_external_agent_count=args.agents)
3. plan = QuestionPlan(retrieval_mode="external",
                       external_research_tasks=[...goals...],
                       target_time_window=args.time_window,
                       reason="external retrieval probe")
4. outcome = await service.retrieve(plan, as_of=<UTC now>)
5. 表示:
   - requested / effective agent count、task 数
   - task ごとの report (status、生成 query、evidence / missing の件数)
   - evidence 一覧 (index、url、title、published_at、claim 等の主要 field)
   - deduplicated_evidence_count、unmet_requirements
6. 正常終了 exit 0。途中の例外はそのまま伝播させ non-zero
```

## Done

- `backend/scripts/probe_question_answering.py` が存在する。
- ruff (format / check) green。`app/` 配下に diff がない。
- API キーを設定した host 環境で手動実行し、実 Tavily / DeepSeek を経由した
  evidence と task_reports が表示される (貫通確認)。
- 実行不能・失敗の場合は原因が stdout / exit code から判別できる。
