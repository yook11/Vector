# 記事補完のSQS・Lambda配送仕様

Status: スライス1の起動・資源管理を実装・検証済み（2026-09-14）。ConsumerとDB確定は実装済み。本書は後続の配送接続と実装スライスを定義し、AWSへの適用完了を意味しない。

## Problem

`article.incomplete_recorded`をSQSから受け取り、既存のArticleCompletionConsumerへ渡し、DB確定後の受信完了または再配信へ対応付ける。待機指示、Lambdaの残り時間、資源管理を配送側で扱う。

## Evidence

- [Consumer仕様](./article-completion-consumer.md)と[Consumer](../../backend/app/collection/article_completion/consumer.py): DB確定・先勝ち・失敗判断を所有し、SQS操作は行わない。
- [取得イベント](../../backend/app/collection/article_acquisition/events.py): `article.incomplete_recorded`、schema version 1、正の`source_id`と`incomplete_article_id`を持つ。
- [SQSレコード](../../backend/app/lambda_handlers/sqs/records.py): messageIdと本文を扱うが、現在は可視性変更に必要なreceiptHandleを保持しない。
- [AIの資源管理](../../backend/app/lambda_handlers/article_analysis_lifecycle.py): 初期化・解放の参照元だが、AIクライアントやAPIキーへの依存をそのまま補完へ持ち込まない。
- [外部HTTP](../../backend/app/http/external.py)、[ソース型](../../backend/app/collection/sources/article_source.py)、[取得ツール](../../backend/app/collection/article_acquisition/tools/reader_tools.py): HTTP設定は独立しているが、ソースの型参照からアプリ全体の設定を読み込む経路がある。
- [HTML抽出](../../backend/app/collection/article_completion/html_extraction.py): 現在は抽出器のプロセス内重複判定を利用し、記事・試行ごとの独立を保証していない。
- [キュー設定](../../infra/aws/outbox_relay.tf): 補完キューは標準キュー、可視性30秒、保持14日、DLQ未設定。[キュレーションLambda](../../infra/aws/curation_consumer.tf)は120秒・バッチ1件で、今回の値へそのまま転用しない。

上記はリポジトリ上の現状であり、稼働中のAWS環境は確認していない。

## 確定した設定

| 項目 | 設定・意味 |
|---|---|
| Lambdaの実行期限 | 最大10分（600秒） |
| バッチ件数 | 最大10件 |
| バッチ内の実行 | 逐次処理 |
| バッチ収集待ち | 0秒 |
| 次の記事の開始条件 | Lambdaの残り実行時間が75秒以上 |
| 1件全体の期限 | 今回は追加しない |
| HTTP取得期限 | 既存のrobots 10秒、記事30秒を維持 |
| 配送側の個別待機上限 | 11時間（39,600秒） |

75秒は次の処理を開始するための余裕であり、1件の完了時間の保証ではない。HTML抽出・構築・DB処理はHTTP取得期限に含まれず、10件すべてを1回で完了する保証も設けない。既存の同期抽出をasyncioのタイマーだけで強制中断できるとは扱わない。

## 入口・検証・結果の対応

入力は既存Outboxのenvelopeを使い、event ID・event type・schema version・発生日時・payloadを既存AI入口と同様に検証する。event typeは`article.incomplete_recorded`、schema versionは1とする。JSONの不正、重複キー、非標準数値、payloadの型・必須項目・余分な項目を検証し、新しいイベント形式は作らない。

payloadの両IDは正の整数として検証する。Consumerへは`incomplete_article_id`を渡し、ソース・記事内容・URLはDBの情報を正とする。イベントのsource_idでDBのソースを上書きせず、一致確認のためだけのDB照会も追加しない。

| 入力・Consumerの結果 | 配送の扱い |
|---|---|
| CompletionSucceeded | 失敗一覧へ含めず受信完了 |
| CompletionNotRequired（全reason） | 失敗一覧へ含めず受信完了 |
| CompletionFailed + CloseArticleCompletion | closed等の確定をConsumerに委ね、失敗一覧へ含めず受信完了 |
| CompletionFailed + RetryArticleCompletion | messageIdをbatchItemFailuresへ含める |
| 本文・イベントの検証失敗 | 当該messageIdを失敗一覧へ含め、Consumerを呼ばない |
| 個別処理で配送側の通常例外 | 当該messageIdを失敗一覧へ含める |
| 初期化失敗、一覧の構造不正、ID欠如・重複などで失敗一覧を安全に作れない | 呼び出し全体を失敗させる |
| 時間不足による未着手 | 未着手の全messageIdを失敗一覧へ含める |

部分バッチ応答には`ReportBatchItemFailures`を設定する。不正イベントから記事をclosedにせず、配送側でHTTP分類やDBの確定をやり直さない。受信完了はLambdaの成功した部分応答に任せ、独自のメッセージ削除を追加しない。

## Retry-Afterの配送契約

工程分類が返した`RetryArticleCompletion.retry_at`だけを使い、生のヘッダーを配送側で再解釈しない。元例外・判断結果・元のretry_atは変更しない。

1. retry_atがなく、または設定時点で経過済みなら可視性を変更せず、通常の再配信設定に任せる。
2. 未来のretry_atがあれば、その時点からの残り秒数を切り上げ、最大39,600秒で`ChangeMessageVisibility`を要求する。
3. 成功しても当該messageIdは部分バッチ失敗一覧に含め、受信完了にはしない。
4. 可視性変更が失敗した場合も失敗一覧に残し、待機を設定できなかったことを配送ログへ記録する。

短い個別待機が後続記事の処理中に切れないよう、可視性の変更はバッチの処理を打ち切った後、応答を返す段階で行い、残り時間を再計算する。可視性操作と資源解放にも時間が必要なため、AWSクライアントの通信待ち・SDK再試行を終了処理の時間予算に収める。具体的なクライアント設定はスライス4で根拠とともに定める。

11時間はAWSの上限ではなく、今回選ぶ配送上の上限である。長いRetry-Afterより早い再試行を許容し、12時間を超える待機を別のスケジューラー・再投入・DB時刻管理で実現しない。これは、従来の「配送でも有効な待機時刻を短縮しない」方針の変更であり、純粋な分類関数が返す時刻は引き続き短縮しない。

AWSの上限12時間は今回のReceiveMessage要求から数える。`ApproximateFirstReceiveTimestamp`を今回の受信時刻とみなさず、Lambda開始時刻を厳密な受信時刻ともみなさない。11時間に抑えても設定成功を無条件には保証せず、AWSが拒否した場合は上記の失敗処理を行う。

receiptHandleは現在の配送から取得し、Queue URLは信頼できる設定から取得する。イベント本文から操作先を選ばない。個別可視性は次回受信へ引き継がれず、実際の再処理時刻や重複排除を保証しない。同じ記事の別メッセージや救済投入、同じサイトの別記事まで停止する仕組みではない。

根拠: [可視性変更API](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ChangeMessageVisibility.html)、[12時間の起点](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/best-practices-processing-messages-timely-manner.html)、[受信属性](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ReceiveMessage.html)。

## 残り時間と資源管理

各記事を開始する直前にLambda contextの残り実行時間を確認する。75秒未満なら新しい記事を開始せず、未着手分を失敗一覧へ追加して待機設定・応答・資源解放へ進む。時間不足そのものを記事の補完拒否として監査・closed化しない。

呼び出し単位で必要な設定を検証し、RDS IAM認証・Collect用DBエンジン・セッション生成関数・SQSクライアントを用意する。HTTPクライアントは既存どおり記事取得ごとに生成し、robotsと記事で共有して閉じる。取得中にDB接続・トランザクションを保持しない。

取得ツールの設定参照をCrossref readerの生成時まで遅らせ、補完の起動ではAI APIキー・フロントエンド・Crossref等の無関係な設定を要求しない。仮の設定値を本番へ足して解決しない。必須のプロキシ設定は起動段階で検証し、記事ごとの失敗へ流さない。既存の宛先保護・プロキシ必須条件は維持する。

初期化途中の失敗も含め、取得済み資源を終了時に解放する。終了処理の通常例外は記録しても確定済みの個別結果を変更しない。外部キャンセル・プロセス終了を通常失敗へ丸めない。Lambdaの強制終了では応答・後始末を保証できず、再配送時は既存のDB状態確認と先勝ちで扱う。

### スライス1の実装境界

[article_fetch_lifecycle.py](../../backend/app/lambda_handlers/article_fetch_lifecycle.py)の`open_article_fetch_consumer()`を、外部から記事を取得する工程の共通起動・終了処理とする。プロキシ設定の検証、呼び出し専用RDS署名クライアント、DBエンジン、セッション生成関数を管理し、工程の生成関数へ渡す。AI用のライフサイクルには依存しない。

[補完のcomposition](../../backend/app/lambda_handlers/completion/composition.py)は実Consumerを組み立て、配送側で所有するSQSクライアントとともに貸し出す。HTTPクライアントの寿命は記事ごとのままとし、SQSを記事取得の共通資源やConsumerへ持ち込まない。配送側へ渡す型は`SqsMessageVisibilityClient` Protocolとし、必要な`change_message_visibility()`のキーワード引数だけを定義する。戻り値を使わないためobjectとし、SDKクライアントのcloseは生成側が担当する。

[記事取得用Engine](../../backend/app/db/engine.py)はNullPoolを使い、セッション終了時に物理接続も返却・切断する。接続ごとのIAM署名と既存TLS設定を維持し、SQL待ちと接続待ちの設定は各5秒とする。DBエンジンを生成するだけでは接続しないため、起動成功をDBの到達性・権限の確認済みとは扱わない。

SQSクライアントは接続・読取待ち各5秒、SDKの総試行回数1回とし、環境変数のプロキシ・endpoint上書きを採用しない。実際の可視性操作とバッチ終了時間の検証はスライス4に残す。共有資源・配送資源の通常の解放障害は診断し、元の結果や外部キャンセルを置換しない。

起動と解放の重要なテストを先行定義し、配送側の解放障害も含む13ケースで、最小環境の別プロセスで実Consumer・DBエンジン・SDKクライアントを生成・解放する保証、プロキシ未設定・不正、途中失敗、外部キャンセル、解放・診断の二次障害を確認する。外部通信・DB操作はこの起動テストでは行わない。

根拠: [SQLAlchemy NullPool](https://docs.sqlalchemy.org/en/20/core/pooling.html#switching-pool-implementations)、[Botocoreの待ち時間と総試行回数](https://docs.aws.amazon.com/botocore/latest/reference/config.html)。

スライス1の検証結果: Ruff lint・format成功、単体6,988件（新規13ケースを含む）、DB統合1,475件、ローカルテスト101件が成功した。一時DB・Redis・ネットワークの削除を確認した。旧Taskiq・既存AIのライフサイクル・補完Consumer本体・DB schema・AWS設定は変更していない。スライス2以降は未実装。

## 抽出の独立性

記事内の重複本文の除去は維持し、別記事や過去の試行によるキャッシュで今回の抽出が空になったり素材が欠落したりしないようにする。同じHTMLの再処理でも、同じ抽出条件なら過去の実行履歴に依存しない。

現在の依存バージョンで利用できるAPIを確認し、逐次実行を前提として実装する。キャッシュの具体的な構造・内部呼び出し順は仕様として固定しない。並列抽出・依存更新・抽出品質やソース別ポリシーの見直しは追加しない。

## 配送側の記録

配送の診断はLambdaの構造化JSONログからCloudWatch Logsへ出し、ConsumerのDB監査とは分ける。

- 検証できたmessage ID・event ID・未完成記事ID、工程名、処理結果・正常終了理由、判断code、requires_investigationを明示的に選ぶ。
- 待機の元のretry_at、要求した秒数、API成功時に設定できた秒数、上限適用の有無、設定失敗を区別する。
- 入力不正、初期化・解放・可視性変更の失敗、時間不足による未着手を区別する。
- 本文・イベント全体・生のヘッダー・receiptHandle・資格情報・例外の自由文は出さない。必要な例外情報は型名と選択した安定コードに限る。

ログ出力の通常例外によって元の結果や待機判断を変えない。調査対象のフラグを記録するが、通知・アラート・新規メトリクス・監査schema変更は含めない。

## AWS接続の境界

スライス5でLambda、イベントソースマッピング、ログ、必要なIAM・ネットワーク経路・DLQを構成する。補完キューの現在の可視性30秒をそのまま使わない。Lambda600秒・バッチ収集待ち0秒に対して、AWS推奨の6倍を満たす通常可視性は3,600秒以上とする。これは個別待機上限11時間とは別の設定である。

DBは既存Collect権限を使い、SQSは対象キューに必要な受信・削除・属性参照・可視性変更権限へ絞る。AI用の資格情報や権限を流用しない。DB schema・権限の変更は予定しない。

DLQへの移動とDBを同期しない。DLQの記事も非closedなら救済再投入を許容し、人の対応まで止める要件は追加しない。保持期間、DLQの受信回数、同時実行数・メモリ、接続先環境と切替手順はスライス5で具体化する。既存リソース値や本番への適用を、本書の作成だけで変更しない。

根拠: [LambdaとSQSの設定](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)、[部分バッチ応答](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)。

## Invariants

1. 完成またはclosedのDB確定より先に受信完了にしない。先に確定した状態を上書きしない。
2. 再試行・未着手・入力不正・配送障害を記事のclosed化へ変換しない。
3. ConsumerとエラーへSQS操作・receiptHandle・配送ログの責務を持ち込まない。
4. 元のretry_atを保持し、実際に設定できた待機を区別する。設定失敗を成功として扱わない。
5. 記事の抽出結果は別記事・過去試行のキャッシュに依存しない。
6. 旧Taskiqの実行接続・失敗分類・DB確定・品質条件・宛先保護を維持する。

## 実装スライスと重要なテスト

各スライスで重要な振る舞いのテストを先に置き、実装・検証を完了してから次へ進む。型定義だけ、継承関係、内部の呼び出し順、既存分類表・品質条件を重複検証するテストは作らない。

| 順序 | 実装範囲 | そのスライスの完了条件 |
|---|---|---|
| 1 | 起動時の設定依存と資源管理 | 無関係な設定なしで起動できる。必須設定の欠如は起動失敗になる。初期化途中の失敗・終了・キャンセルで取得済み資源を解放する |
| 2 | 抽出の重複判定の独立 | 同じHTMLの再処理と、別記事を先に処理する代表例で素材が欠落せず、記事内の重複除去を維持する |
| 3 | 入力検証とConsumerへの接続 | 実Consumer・実DBの代表例から部分バッチ応答までを検証し、正常終了・closed確定は受信完了、再試行・個別入力不正は当該レコードの失敗となる |
| 4 | 個別待機と残り時間 | 元時刻の保持、11時間上限、待機変更失敗、残り75秒の境界、未着手分の再配信、終了処理を確認する |
| 5 | AWS設定と接続検証 | 10分・最大10件・逐次処理の構成、部分バッチ応答、通常可視性、IAM・ネットワーク・DLQの接続が成立する |

配送ログは各スライスの該当処理と一緒に追加し、ログ障害で応答が変わらないことと、出してはいけない情報を代表例で確認する。スライス4の時計とAWS応答は制御し、実際に10分・11時間待つテストは作らない。Consumerの既存ローカルテストは独立した保証として残す。

実装変更は`check`に従いRuff lint・format、単体（`-m 'not integration'`）、`make test-integration`、`make test-local`を実行する。インフラ変更時は該当する設定検証も行い、未実行項目は理由を残す。一時テスト環境を削除する。

## Non-goals

- 今回の仕様作成でのproduction code・テスト・Terraform変更、AWS適用。
- 旧Taskiqの撤去・実行経路の切替、救済処理の実装、取得工程全体のイベント駆動化。
- 1件全体の追加タイムアウト、バッチ内並列化、12時間を超える待機の実現。
- 新しい排他制御、DB schema・監査schema・分類表・品質条件・ソース別方式の変更。
- 通知・メトリクスの追加、抽出依存の更新。

## Done

仕様作成は、確定した設定・結果の対応・責務・制約・スライスごとの完了条件・後続で具体化するAWS設定を区別し、Consumer仕様と実装プランから参照できれば完了する。

実装の完了は各スライスの検証結果で別途記録する。コードの検証成功とAWSへの適用・運用切替を同一視せず、本書のStatusを実装済みへ先回りして変更しない。
