# AI無料枠ゲートと専用スキップ分類の撤去

Status: Implemented（2026-09-09）

## Problem

無料枠向けの回数制限が有料利用後も本文整形を待機・先送りし、未処理の記事を正常終了として扱っていたため、事前ゲートと専用の観測項目を撤去する。

## Evidence

- 3工程のTaskiq task、AI call spec、provider adapter、起動時のcomposition。
- Logfireの記事工程span・処理結果metricと、AI provider失敗通知のテスト。
- 内部検索のquery embeddingは同じポリシー型を保持していたが、ゲートを呼んでいなかった。

## Invariants

- Ready構築後に既存Serviceへ進み、処理済み・不正入力・保存競合の判定を維持する。
- 実APIの429・通信障害・5xx・利用枠枯渇・残高不足の分類、再試行、監査、通知を維持する。
- AI adapterの読み取り専用`provider`は既存`SPEC.provider`を返し、失敗通知に同じ事業者名を渡す。
- query embeddingのモデル・次元・task type・キャッシュ識別子を変えない。
- workerの同時実行上限、DB pool、timeout、backfillの投入上限・日次予算・holdを維持する。

## Implementation

- 日次・分次ゲート、専用Redis limiter、3工程の早期終了と起動時配線を削除する。
- `rate_limit_policy`を設定・adapterから削除し、3工程の基底interfaceを`provider: str`へ置き換える。
- 未使用だったquery embeddingのpolicy公開も削除する。
- ゲート専用ログ・`vector.analysis.rate_limit_gate_skipped`・span resultの`rate_limited`を新規出力しない。
- 実APIエラーの`AIProviderRateLimitedError`と、処理済み・競合の`skipped`は維持する。

## Non-goals

AI日次予算の新設、DB schema・外部API・認証・ユーザー利用制限の変更、Lambda consumer、SQS切り替え、デプロイは含めない。Terraform・Redis ACLは維持し、`ratelimit:*`専用権限の削除は後続作業とする。既存キーはTTLで失効させ、Redis基盤や過去の監査データを削除しない。

## Done / Verification

ゲートなしで正常処理が進み、既存の拒否・重複防止・実API障害の契約を保つことをテストで確認する。ゲート専用テストを撤去し、adapter・fake・起動配線・spanのテストを更新する。backendのlint・format・単体テスト・実DB統合テストを実行して結果を記録する。

検証結果（2026-09-09）:

- backend実装と変更したテストのRuff lint・format checkが成功。
- `uv run pytest tests/ -m "not integration" -x -q`: 5,975件成功。
- `make test-integration`: 1,290件成功・22件skip、テスト用PostgreSQL・Redisの後片付けも成功。
- `git diff --check`が成功し、実行コードに旧ゲート参照がないことを確認。
- 全testsを一括lintした際には、未変更の`tests/queue/test_queue_health_task.py`に既存の未使用`MagicMock` import（F401）があったため、対象外のコードは変更せず、変更したテストをlintした。
- 外部AIへの実リクエスト、本番適用は未実施。

## 適用時の確認

関連workerを更新し、既存の監視で処理量・DB負荷・429を確認する。無料枠ゲートの待機がなくなるため処理量は増え得るが、同時実行上限は維持される。問題時は旧イメージへ戻す。本作業ではデプロイしない。
