# Research画面の180秒ローカル期限を撤去する

Status: 実装済み — 2026-09-03

親仕様: `backend/specs/agent-run-deadline-result-acceptance.md` の Slice 4。
このスライスはfrontendの旧時計撤去のみを扱う。

## Problem

画面が質問の `createdAt + 180秒` を独自に判定し、DBでrunがactiveのままでも
`isRecoveryPending` により表示と読み上げを切り替える。
経過時間だけによる特別表示を撤去し、runの状態と通信の状態に従って表示する。

## Evidence

- `live/controller.ts` が180秒の期限・timer・`isRecoveryPending`を持つ。
- `hooks/useResearchRunLiveState.ts` が期限用の `createdAt` をcontroller identityに含める。
- `ResearchThreadLiveBoundary.tsx` が初期表示でも同じ期限を計算し、専用noticeを表示する。
- `LiveAnswerDraft.tsx` がこのflagで下書きの見出しと空状態を切り替える。
- `ResearchThreadView.tsx` が質問の `createdAt` を上記境界へ渡す。
- `deadline_exceeded` のSSE・GET受理と時間切れ表示は既に実装されている。

## Invariants

- 180秒以上の経過だけでrun status・通常表示・下書き・通信方式を変えない。
- 古い質問を初めて開いた場合も、`createdAt`だけで特別表示へ切り替えない。
- controller identityは `runId` とし、同じrunでの再描画で接続や下書きを作り直さない。
- SSE接続・再接続中はpollせず、CLOSED等でpolling-onlyへ移行する既存契約を維持する。
- pollingの成功間隔、失敗backoff、visibility制御、terminal後のrefreshとretryを維持する。
- 通信劣化で `draftMode === "suppressed"` になった場合の状態確認表示は維持する。
- DBで確定した `deadline_exceeded` をSSEまたはGETで受理した際の既存動作を維持する。
- APIの `createdAt` と、確定回答のcontent keyに使うassistant messageの `createdAt` を維持する。

## Non-goals

- backendのdeadline fence、Taskiqの180秒、sweeper、DB schema、公開APIの変更。
- 再接続回数・通信timeout・polling方式の再設計。
- 表示文言の改善は時計撤去と分け、下記の後続変更で扱う。
- 旧 `failed / stale` の語彙削除、認証・認可、新しい依存の追加。

## Done

1. frontend production codeに180秒ローカル期限、専用timer、`isRecoveryPending`が残らない。
2. 期限だけに使う `createdAt` のprops・hook入力・controller入力を撤去する。
3. 初期表示・時間経過・下書き維持の新しい期待で旧実装のredを確認し、実装後にpassする。
4. frontendのlint・型・テストが通り、既存通信経路とterminal処理を維持する。

この仕様は `backend/specs/agent-run-timeout-terminalization-slice.md` の
180秒ローカル期限とrecovery-pending表示の契約を置き換える。
旧仕様の履歴は維持し、通信劣化時の表示は引き続き別の責務とする。

## 後続変更: 終状態を受け取った後の表示文言

- Problem: completed後の回答取得待ちと時間切れの文言を、合意した利用者向け表現に揃える。
- Evidence: `LiveAnswerDraft.tsx` のcompleted表示2か所と、`ResearchThreadLiveBoundary.tsx` の読み上げ・時間切れnotice。
- Invariants: completed後の取得待ちは「回答を生成しています」、`deadline_exceeded`は「回答の生成に時間がかかり、完了できませんでした。少し時間をおいて、もう一度お試しください。」とし、画面と読み上げを揃える。
- Non-goals: 状態遷移、通信制御、下書きの保持・非表示条件、通常の生成中表示、その他の失敗文言の変更。
- Done: 既存の表示テスト・E2Eの期待文言を更新し、frontendのlint・型・単体テストが通る。
