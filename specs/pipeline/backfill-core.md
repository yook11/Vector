# Backfill本体 — 日次上限・stage holdなしのSQS再投入

## Problem

旧Taskiqに依存するbackfillを、工程別Lambdaから呼び出せる本体へ分離する。curation・投資判定・embeddingの未完了データをDBの現在状態から抽出し、期限切れ整理とSQS再投入を行う。

## Evidence

- `app.backfill.repository.PipelineBacklog`：新経路のイベント対象抽出・件数観測・期限切れ整理の照会。旧 Taskiq Curation の対象取得は `app.queue.helpers.backlog` が担う。
- `app.backfill.targets`：再投入する保存済み事実。
- `app.backfill.policy`：元記事の年齢窓と一回の処理上限。
- 各consumerの共有イベント型と、共通の本文生成関数・`MessageSender`。
- 旧maintenanceテストと`tests/backfill/`の単体・DB統合テスト。

## Interface

`app.backfill.service`に`backfill_curations`・`backfill_assessments`・`backfill_embeddings`・`backfill_completions`を用意する。いずれも`session_factory, sender_factory, *, route, build_message, enabled, now`を受け取り、正常時は`None`、続行不能な障害は例外を返す。`now`はタイムゾーン付き日時を呼び出し元から渡す。

DBと送信クライアントの生成・終了、設定取得は呼び出し元の責任とする。工程ごとの配送定義・既存本文builderと`MessageSenderFactory`を本体へ渡し、`_dispatch`内で本文生成関数を1件ずつ呼んでから正常分を`sender.send_batch`で送る。新本体にTaskiq・Lambda・Redisの依存を持たせない。

## Invariants

- 順序：有効設定 → 期限切れ整理 → backlog総数と対象の取得 → 最大10件ずつメッセージへ変換 → 正常分を送信 → 結果記録。
- 無効ならDB照会・整理・送信を行わない。日次予算とstage holdは新経路に設けない。
- 全工程で元記事の`created_at`を基準に、`now - 7日 <= created_at < now - 30分`を救済する。古い記事を優先し、一回50件まで送る。同時刻は対象ID順とする。
- `created_at < now - 7日`は期限切れとする。curationは一回200件まで削除し、投資判定・embeddingは一回50件まで既存の救済除外行を作る。補完は一回50件まで未完成行を`closed`へ更新し、行は残す。
- 整理は一記事一トランザクションとし、親となる対象行をロックした後に新たな照会で未完了・期限切れを再確認する。完了・削除・除外済みならスキップする。
- 対象・件数のSELECTは同一の完了判定を使うが、並行更新による観測総数と抽出件数の差は許容する。
- SQS送信時にはDBセッションと行ロックを保持しない。処理中状態・SQS・DLQは照会しない。

## 再投入契約

| 工程 | イベント | payload | occurred_at |
|---|---|---|---|
| curation | article.analyzable_created | analyzable_article_id | 元記事created_at |
| 投資判定 | article.curated_signal | analyzable_article_id・curation_id | curationのextracted_at |
| embedding | article.assessed_in_scope | curation_id・analyzed_article_id | 分析結果のanalyzed_at |
| 補完 | article.incomplete_recorded | source_id・incomplete_article_id | 未完成行のcreated_at |

対象ごと・再投入ごとに新しいUUIDをevent_idに使う。既存Outbox行の再送ではなく、DBに残る事実から受信契約に沿って再構成する。Outboxの検索・追加・状態更新は行わない。

EventInvalidErrorによる変換失敗（種別違いを含む）は該当イベントを除外し、全件変換失敗のバッチはSenderを呼ばず、次のバッチへ進む。本文生成中の想定外例外では同じバッチのどのメッセージも送らない。

Backfillは`(EventEnvelope, EventMessage)`の組をSenderへ渡し、Senderは元の対象と`MessageAccepted | MessageSendError`の組を入力順で返す。結果型は`app.outbox.relay.sender`で定義し、BackfillはAWS側の型をimportしない。`MessageAccepted`の件数だけを集計し、個別失敗があっても後続バッチを送る。Sender初期化時の`MessageSendError`は当該バッチの成功0件として次へ進み、同一実行内の追加リトライはしない。本文生成・送信から伝播したその他の例外は実行全体の失敗とする。

送信成功は送信先の受付のみを意味し、DBに送信済み状態を持たせない。未完了なら次回も送る。入力と結果の対応は`tests/aws/sqs/`でSenderの責任として検証し、Backfill側は分割・変換失敗の除外・成功件数・部分失敗時の継続と例外の伝播を検証する。

## Compatibility / Non-goals

旧Taskiq Curationタスクの対象取得・件数観測は `app.queue.helpers.backlog` に残す。期間判定・整理・metric・一回上限は新経路の共通部分を参照する。Curationの日次予算・stage hold・Taskiq投入・定期実行は残す。旧Assessment・EmbeddingのTaskiq backfillは撤去し、Lambdaからの再投入と共有cleanupを維持する。

Lambda入口、Scheduler、AWS権限・ネットワーク、デプロイ、旧経路の停止・撤去は後続スライスとする。DB schema・consumer・受信契約・新規dependencyは変更しない。後続Schedulerでは既存の30分間隔と工程別オフセットを引き継ぐ。

## Done

全3工程の対象抽出、期限切れ整理の原子性と競合、イベント内容、分割送信、失敗の伝播と継続を検証し、共有Curation経路のテストを維持する。`/check`のlint・format check・単体・DB統合テストが成功する。AWS上の動作確認は今回の完了条件に含めない。

## 検証結果（2026-09-14）

- 全単体テスト6,911件、全DB統合テスト1,475件が成功した。
- Ruff lint・format check、差分の空白チェックが成功した。
- 新本体の単独importを検証し、embedding監査の型注釈専用importをTYPE_CHECKINGへ移して循環参照を解消した。
- 旧削除metricのテストは実DBの整理件数と照合する形に更新し、計測の保証を維持した。
- テスト用DB・Redis・ネットワークは終了処理で削除した。AWS導入と旧経路の撤去は行っていない。
