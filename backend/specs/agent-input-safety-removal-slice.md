# Agent input safety gate 撤去 slice 仕様

更新日: 2026-08-26

実装状況: Implemented — 2026-08-26

## 位置付け

`AnsweringRunner.run()` の先頭 phase である input safety gate（`app/agent/input_safety/`）を撤去し、
入力内容の許可・拒否判定を provider の safety filter へ委ねる。

本 slice は `agent-input-safety-gate-slice.md` を失効させる。同 spec が固定した
Input Safety Agent、`InputSafetyChecker` 契約、`InputSafetyBlocked` による停止制御、
block-only structured log、outcome metric はいずれも残らない。
`agent-answering-runner-boundary-slice.md` §12 の「allow 確定前に後続 runtime を起動しない」
制約も、判定そのものが無くなるため同時に失効する。両 spec は履歴として編集せずに残す。

## Work Definition

### Problem

- この gate は、ユーザーの心理状態に関わる別アプリケーション向けに設計した safety check を
  本アプリへ流用したものだった。Vector の用途に合わせて置いた境界ではない。
- Vector の research agent は tool を実行せず、外部への副作用を持たず、回答は入力者本人にしか
  届かない。危険な回答が第三者へ到達する経路がなく、回答の情報価値も汎用検索エンジンを超えない。
  この構造で gate が減らす marginal risk は、provider の safety filter が既にカバーする範囲と
  ほぼ重なる。
- 一方コストは確定的で、全 run が回答前に LLM 判定を 1 回追加で払い、誤ブロックの余地も残る。

### Evidence

- `app/agent/running/answering_runner.py` は `run()` の先頭で checker を呼び、
  block 時に `InputSafetyBlocked` を raise していた。
- `app/queue/tasks/agent_run.py` はこれを catch し、`_mark_policy_blocked()` で
  `policy_blocked` terminal へ遷移させていた。`policy_blocked` を書き込む経路はこの 1 本だけ。
- `INPUT_SAFETY_TEXT_CHAR_CAP` による 1,000 文字切り詰めは checker への入力専用で、
  `context_preparer.prepare()` には切り詰めない質問が渡っていた。撤去しても後続の入力長は変わらない。
- `progress_stage` は nullable、DB CHECK は `IN (...)` の許可リスト。値を報告しなくなっても
  制約違反にならない。frontend の `stageRank` は `null`=0 の単調前進判定で、
  初回報告が `context_resolution` へ変わっても前進として受理される。
- `app/analysis/prompt_safety.py::sanitize_for_untrusted_block()` は 7 工程が共有しており、
  input safety とは独立に prompt の命令境界を守っている。

### Invariants

- 入力の形式検証（strip / 空文字 / 1,000 文字上限）は `ResearchQuestion` が引き続き担う。
- prompt の untrusted boundary は維持する。撤去対象は内容の許可・拒否判定だけである。
- `AnsweringRunner.run()` の最初の意味的 phase は `context_resolution` になる。
  最初に報告される progress stage も `context_resolution` になる。
- `policy_blocked` run status と `safety_check` progress stage の語彙は DB・API・SSE・
  frontend に残す。到達不能になるだけで、公開契約は変更しない。
- 公開 API の response shape、DB schema、migration、frontend を変更しない。
- 撤去した概念専用のテストを「起動しない」へ反転して残さない。

### Non-goals

- `policy_blocked` / `safety_check` 語彙の削除。DB CHECK 制約、API union、SSE terminal、
  frontend の分岐はいずれも本 slice の対象外とする。
- Gemini の safety settings の見直し。現行設定はまとめて設計したものではないため、
  委任先として妥当かを別途評価する。本 slice では設定を変更しない。
- 回答内容に対する出口側の制約（投資助言に該当しない旨の表示など）。
- prompt injection / jailbreak 対策の再設計。

### Done

- `app/agent/input_safety/` が存在しない。Runner、worker、composition、probe script から
  checker への依存が消える。
- `AnsweringRunner.__init__` が `input_safety_checker` を持たない。
- worker が `InputSafetyBlocked` を catch せず、`_mark_policy_blocked()` を持たない。
- 語彙契約テスト（`test_phase_vocabulary_contract.py` /
  `test_progress_stage_vocabulary_contract.py`）と `tests/agent/runs/test_policy_blocked.py` が
  無変更のまま green である。
- `/check`（backend lint / format / unit / DB integration）が pass する。

## 撤去の帰結

`policy_blocked` 語彙を残すため、次の 2 つは本番から呼ばれないコードになる。意図的に残す。

- `AgentRunRepository.mark_policy_blocked` — 語彙を残す方針と整合する。
  repository 層の CAS 遷移を検証する既存テストがそのまま所有し続ける。
- `AIProviderInputRejectedError.is_safety_rejection` /
  `AIProviderOutputBlockedError.is_safety_rejection` — analysis 側の共有エラー契約であり、
  input safety の都合で削らない。

## 記録の置き換え対応表

| 消える防壁・記録 | 置き換え先 |
| --- | --- |
| Input Safety Agent の application policy 判定 | provider（Gemini）の safety filter |
| policy block の structured log | 置き換えない（block 自体が発生しない） |
| metric `vector.agent.input_safety.outcome` | 置き換えない（consumer 不在） |
| 工程順「safety → prepare → phases → planner → answerer」の固定 | `tests/agent/running/test_answering_workflow.py` が `prepare` 以降を同等に固定 |
| block 時に後続ポートを起動しないことの固定 | `test_run_does_not_start_answering_when_context_preparation_fails` が「先頭工程の失敗で後続が走らない」を担保 |
| `InputSafetyBlocked` で run span を ERROR にしない契約 | `test_generation_stopped_closes_run_span_without_error` が `AnswerGenerationStopped` について同じ span 契約を維持 |

## 検証

- `/check`（backend lint / format / unit / `make test-integration`）。
- 無変更のまま green であるべきテスト:
  `tests/agent/test_phase_vocabulary_contract.py`、
  `tests/agent/test_progress_stage_vocabulary_contract.py`、
  `tests/agent/runs/test_policy_blocked.py`、
  `tests/test_agent_policy_blocked_migration.py`。
