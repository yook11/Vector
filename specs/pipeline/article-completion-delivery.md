# 記事補完のSQS・Lambda配送仕様

Status: スライス1〜4の起動・資源管理、抽出の独立、入力検証・Consumer接続、個別待機・残り時間管理を実装・ローカル検証済み（2026-09-14）。ConsumerとDB確定は実装済み。スライス5のAWS設定と補完Relayを実装中。AWSへの適用・実接続は未確認。

> 2026-09-20: digest入力と`*_state`入力は廃止した。以下は構築時の記録で、現在の扱いは[app rollout](../platform/app-rollout.md)を参照する。

## Problem

`article.incomplete_recorded`をSQSから受け取り、既存のArticleCompletionConsumerへ渡し、DB確定後の受信完了または再配信へ対応付ける。待機指示、Lambdaの残り時間、資源管理を配送側で扱う。

## Evidence

- [Consumer仕様](./article-completion-consumer.md)と[Consumer](../../backend/app/collection/article_completion/consumer.py): DB確定・先勝ち・失敗判断を所有し、SQS操作は行わない。
- [取得イベント](../../backend/app/collection/article_acquisition/events.py): `article.incomplete_recorded`、schema version 1、正の`source_id`と`incomplete_article_id`を持つ。
- [SQSレコード](../../backend/app/lambda_handlers/sqs/records.py): messageIdを全件事前検証し、本文とreceiptHandleは用途ごとに遅延検証する。
- [AIの資源管理](../../backend/app/lambda_handlers/article_analysis_lifecycle.py): 初期化・解放の参照元だが、AIクライアントやAPIキーへの依存をそのまま補完へ持ち込まない。
- [外部HTTP](../../backend/app/http/external.py)、[ソース型](../../backend/app/collection/sources/article_source.py)、[取得ツール](../../backend/app/collection/article_acquisition/tools/reader_tools.py): HTTP設定は独立しているが、ソースの型参照からアプリ全体の設定を読み込む経路がある。
- [HTML抽出](../../backend/app/collection/article_completion/html_extraction.py): スライス2着手時点では抽出器のプロセス内重複判定を利用し、記事・試行ごとの独立を保証していなかった。
- [キュー設定](../../infra/aws/outbox_relay.tf): 補完キューは標準キュー、可視性30秒、保持14日、DLQ未設定。[キュレーションLambda](../../infra/aws/curation_consumer.tf)は120秒・バッチ1件で、今回の値へそのまま転用しない。

上記はリポジトリ上の現状であり、稼働中のAWS環境は確認していない。

## 確定した設定

| 項目 | 設定・意味 |
|---|---|
| Lambdaの実行期限 | 最大10分（600秒） |
| バッチ件数 | 最大10件 |
| バッチ内の実行 | 逐次処理 |
| バッチ収集待ち | 0秒 |
| 次の記事の開始条件 | Lambdaの残り実行時間が60秒以上 |
| 1件全体の期限 | 今回は追加しない |
| HTTP取得期限 | 既存のrobots 10秒、記事30秒を維持 |
| 可視性変更の開始条件 | Lambdaの残り実行時間が20秒以上 |
| 配送側の個別待機上限 | 11時間（39,600秒） |

60秒は次の処理を開始するための余裕であり、1件の完了時間の保証ではない。HTML抽出・構築・DB処理はHTTP取得期限に含まれず、10件すべてを1回で完了する保証も設けない。既存の同期抽出をasyncioのタイマーだけで強制中断できるとは扱わない。

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

短い個別待機が後続記事の処理中に切れないよう、可視性の変更はバッチの処理を打ち切った後、応答を返す段階で行い、残り時間を再計算する。可視性操作と資源解放にも時間が必要なため、AWSクライアントの通信待ち・SDK再試行を終了処理の時間予算に収める。既存クライアントの接続待ち5秒・応答待ち5秒・総試行回数1回を維持する。通信待ちの合計10秒と終了処理の余裕10秒から、設定開始条件を残り20秒以上とする。これらは開始判断であり、SDK通信全体や終了処理の厳密な上限ではない。

11時間はAWSの上限ではなく、今回選ぶ配送上の上限である。長いRetry-Afterより早い再試行を許容し、12時間を超える待機を別のスケジューラー・再投入・DB時刻管理で実現しない。これは、従来の「配送でも有効な待機時刻を短縮しない」方針の変更であり、純粋な分類関数が返す時刻は引き続き短縮しない。

AWSの上限12時間は今回のReceiveMessage要求から数える。`ApproximateFirstReceiveTimestamp`を今回の受信時刻とみなさず、Lambda開始時刻を厳密な受信時刻ともみなさない。11時間に抑えても設定成功を無条件には保証せず、AWSが拒否した場合は上記の失敗処理を行う。

receiptHandleは現在の配送から取得し、Queue URLは信頼できる設定から取得する。イベント本文から操作先を選ばない。個別可視性は次回受信へ引き継がれず、実際の再処理時刻や重複排除を保証しない。同じ記事の別メッセージや救済投入、同じサイトの別記事まで停止する仕組みではない。

根拠: [可視性変更API](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ChangeMessageVisibility.html)、[12時間の起点](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/best-practices-processing-messages-timely-manner.html)、[受信属性](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ReceiveMessage.html)。

## 残り時間と資源管理

各記事を開始する直前にLambda contextの残り実行時間を確認する。60秒未満なら新しい記事を開始せず、未着手分を失敗一覧へ追加して待機設定・応答・資源解放へ進む。時間不足そのものを記事の補完拒否として監査・closed化しない。

呼び出し単位で必要な設定を検証し、RDS IAM認証・Collect用DBエンジン・セッション生成関数・SQSクライアントを用意する。HTTPクライアントは既存どおり記事取得ごとに生成し、robotsと記事で共有して閉じる。取得中にDB接続・トランザクションを保持しない。

取得ツールの設定参照をCrossref readerの生成時まで遅らせ、補完の起動ではAI APIキー・フロントエンド・Crossref等の無関係な設定を要求しない。仮の設定値を本番へ足して解決しない。必須のプロキシ設定は起動段階で検証し、記事ごとの失敗へ流さない。既存の宛先保護・プロキシ必須条件は維持する。

初期化途中の失敗も含め、取得済み資源を終了時に解放する。終了処理の通常例外は記録しても確定済みの個別結果を変更しない。外部キャンセル・プロセス終了を通常失敗へ丸めない。Lambdaの強制終了では応答・後始末を保証できず、再配送時は既存のDB状態確認と先勝ちで扱う。

### スライス1の実装境界

[article_fetch_lifecycle.py](../../backend/app/lambda_handlers/article_fetch_lifecycle.py)の`open_article_fetch_consumer()`を、外部から記事を取得する工程の共通起動・終了処理とする。プロキシ設定の検証、呼び出し専用RDS署名クライアント、DBエンジン、セッション生成関数を管理し、工程の生成関数へ渡す。AI用のライフサイクルには依存しない。

[補完のcomposition](../../backend/app/lambda_handlers/completion/composition.py)は実Consumerを組み立て、配送側で所有するSQSクライアントとともに貸し出す。HTTPクライアントの寿命は記事ごとのままとし、SQSを記事取得の共通資源やConsumerへ持ち込まない。配送側へ渡す型は`SqsMessageVisibilityClient` Protocolとし、必要な`change_message_visibility()`のキーワード引数だけを定義する。戻り値を使わないためobjectとし、SDKクライアントのcloseは生成側が担当する。

[記事取得用Engine](../../backend/app/db/engine.py)はNullPoolを使い、セッション終了時に物理接続も返却・切断する。接続ごとのIAM署名と既存TLS設定を維持し、SQL待ちと接続待ちの設定は各5秒とする。DBエンジンを生成するだけでは接続しないため、起動成功をDBの到達性・権限の確認済みとは扱わない。

SQSクライアントは接続・読取待ち各5秒、SDKの総試行回数1回とし、環境変数のプロキシ・endpoint上書きを採用しない。スライス1時点で未実施だった可視性操作とバッチ終了時間の検証は、後述のスライス4で実施した。共有資源・配送資源の通常の解放障害は診断し、元の結果や外部キャンセルを置換しない。

起動と解放の重要なテストを先行定義し、配送側の解放障害も含む13ケースで、最小環境の別プロセスで実Consumer・DBエンジン・SDKクライアントを生成・解放する保証、プロキシ未設定・不正、途中失敗、外部キャンセル、解放・診断の二次障害を確認する。外部通信・DB操作はこの起動テストでは行わない。

根拠: [SQLAlchemy NullPool](https://docs.sqlalchemy.org/en/20/core/pooling.html#switching-pool-implementations)、[Botocoreの待ち時間と総試行回数](https://docs.aws.amazon.com/botocore/latest/reference/config.html)。

スライス1の検証結果: Ruff lint・format成功、単体6,988件（新規13ケースを含む）、DB統合1,475件、ローカルテスト101件が成功した。一時DB・Redis・ネットワークの削除を確認した。旧Taskiq・既存AIのライフサイクル・補完Consumer本体・DB schema・AWS設定は変更していない。この検証時点ではスライス2以降は未実装。

## 抽出の独立性

新経路の`extract_html_content()`ではTrafilaturaの重複除去を`deduplicate=False`で無効にする。同じHTMLの再処理や、共通の段落を持つ別記事の先行処理を理由として、今回の素材を除外しない。

記事内の文章の繰り返しは許容し、独自の重複除去や記事単位のキャッシュ生成・リセット処理を追加しない。日時解析等の別のキャッシュを全面禁止する仕様ではない。本文抽出・文字コード・日時抽出・統合後の品質条件は維持する。旧Taskiqのスクレイパーの重複除去設定、ライブラリ依存バージョン、ソース別の採用方針は変更しない。

従来の「記事内の重複除去を維持する」方針を変更した。必要性を未確認のままキャッシュの寿命管理を追加せず、繰り返しの文章が実際に問題になる場合に別途対応を検討する。

重要なテストは、実抽出器で同じHTMLを繰り返して素材が欠落しないことと、共通段落を持つ別記事を先に抽出しても対象記事の素材が変わらないことの2ケースに絞る。処理の途中でキャッシュをリセットせず、オプション値・内部キャッシュ構造だけを固定するテストは追加しない。補完ローカルテストのキャッシュリセット用fixtureも削除し、既存の成功・失敗・競合を履歴を消さずに検証する。

スライス2の検証結果: 変更前に失敗した実抽出器の2ケースを含め、単体6,990件、DB統合1,475件、ローカルテスト101件、Ruff lint・formatが成功した。補完fixtureのキャッシュリセットなしで既存の成功・失敗・競合が成立し、一時DB・Redis・ネットワークの削除を確認した。この検証時点ではスライス3以降は未実装。

## スライス3の入力検証とConsumer接続

[補完handler](../../backend/app/lambda_handlers/completion/handler.py)は既存の設定と`open_completion_resources`で呼び出し専用の資源を開き、`SqsRecordBatch`で全IDを検証してから逐次処理する。同期の`handler(lambda_event, context)`が非同期処理を実行し、失敗IDの原文と入力順を保持した`batchItemFailures`を返す。空バッチは空の失敗一覧となる。

[イベント解析](../../backend/app/lambda_handlers/completion/event.py)はSQS本文のJSON解析と重複キー・非標準数値の拒否を担当し、解析結果を[取得工程のイベント契約](../../backend/app/collection/article_acquisition/events.py)の`IncompleteArticleRecordedEvent.from_input()`へ渡す。イベント型が既存Outboxのenvelopeと`IncompleteArticleRecorded`を使い、UUID・種類・version・タイムゾーン付き日時、必須項目・余分な項目・両IDの厳密な正整数を検証する。形式検証と失敗変換・送出は`from_input()`へ集約し、違反の変換処理もイベント型のprivateメソッドに置く。不正時の例外には固定の理由・項目・検証コードだけを保持し、入力値を持つ元の検証例外をcontextへ引き継がない。

本文不正はConsumerを呼ばず個別失敗とし、正常入力では`incomplete_article_id`だけを渡す。成功・処理不要・closed確定は受信完了、再試行判断と配送側の通常例外は当該メッセージの失敗となる。初期化や全ID検証の失敗は呼び出し全体へ伝播し、外部キャンセルを個別失敗へ変換しない。DBの確定やHTTP失敗分類、メッセージ削除・再投入を配送側で行わない。

配送診断は処理結果・不要理由・判断code・requires_investigation・元のretry_atを選択して記録し、入力本文・未知の項目名・receiptHandle・生ヘッダー・例外の自由文を含めない。初期化・解放には既存の資源管理ログを使い、ログの通常障害で応答を変更しない。

`IncompleteArticleEventInvalidError`は取得工程で定義し、`invalid`に入力検証の理由と違反項目を保持する。JSON解析失敗はLambda側の`CompletionMessageJsonInvalidError`で表し、イベント契約の例外は包み直さずhandlerへ伝える。どちらも通常の`Exception`を継承し、`VectorDomainError`・`SAFE_ATTRS`や独自の文字列表現によるログ制御を持たせず、出力する項目は配送recorderが選ぶ。

通常の`Exception`継承への修正後も、Ruff lint・format、単体7,054件、DB統合1,475件、ローカル110件を再実行して成功した。入力検証・部分応答・配送診断のテストは変更せず、継承構造だけを固定するテストは追加していない。

`from_input()`への集約では、既存の形式検証テストをイベント型の直接呼び出しへ移し、未実装による失敗を確認してから接続した。JSON固有の拒否は解析関数、形式の成立条件と検証例外の変換はイベント型で確認する。変更後のRuff lint・format、全単体7,067件、DB統合1,481件、ローカル111件が成功し、一時テスト環境の削除を確認した。

先行テストはイベント解析・handlerの未実装による失敗を確認してから接続した。単体は入力検証・失敗範囲・応答・診断を確認し、既存の最小環境起動テストを実handlerの空バッチ応答まで拡張した。[配送の実DBテスト](../../backend/local_tests/completion/test_completion_delivery.py)は混在バッチ、closed確定・closed済み・同URL完成済みの受信完了、不正イベントでの記事保持、429後の再受信、closed確定失敗、完成後の再受信、イベントと異なるDBのsource_idを使う処理の9ケースを担当する。実handler・資源管理・Consumer・抽出・Repository・IAM接続経路を使用し、HTTP・署名器・SQSクライアントだけを外部境界で置き換える。

スライス3時点では、Retry-Afterによる可視性操作・残り実行時間の確認・未着手分の集約をスライス4、AWS設定・DLQ・実接続をスライス5の対象とした。Consumer本体・DB schema・権限・依存・旧Taskiqは変更しない。

スライス3の検証結果（2026-09-14）: Ruff lint・format、単体7,054件（追加64件を含む）、`make test-integration`のDB統合1,475件、ローカル全110件（追加の配送9件を含む）が成功した。ローカル全体の初回収集では別工程との同名テスト衝突を検出し、補完専用のファイル名へ変更して全110件の収集・実行を確認した。実装後にテスト指針の更新を反映して検証目的ごとにケースを整理し、最終状態の単体・ローカル全体を再確認した。`git diff --check`と一時DB・Redis・ネットワークの削除を確認した。既存の依存ライブラリ・Logfire由来の警告は残る。AWS実接続・設定適用と可視性変更・残り時間の検証は、スライス4・5の対象のため未実施。

イベント契約を取得工程へ移し、他の3受信工程も含めて例外と配送処理の責務を整理した。[4工程の整理内容](sqs-message-validation.md#イベント契約とsqs受信の責務整理2026-09-14)を参照する。検証結果（2026-09-14）: app全体と変更テストのRuff lint・format、全単体7,078件、`make test-integration`のDB統合1,481件、`make test-local`のローカル111件が成功した。`git diff --check`と一時DB・Redis・ネットワーク・ボリュームの削除を確認した。既存の非推奨・Logfire関連の警告は残る。AWS実接続・設定適用、可視性変更・残り時間の検証はスライス4・5の対象として未実施。

EventInvalidへの命名統一後の最終検証（2026-09-14）: コミット対象だけを反映した検証用チェックアウトで、app全体・変更PythonファイルのRuff lint・format、全単体7,048件、DB統合1,475件、ローカル110件が成功した。別件の未コミットテスト整理は含めていない。差分チェックと一時DB・Redis・ネットワーク・ボリュームの削除を確認した。AWS実接続・設定適用、個別待機・残り時間管理はスライス4・5に残る。

## スライス4：再配信待機と時間予算

`RetryAt(value: datetime)`を補完工程に置き、タイムゾーン付き日時をUTCへ正規化して不変に保持する。`remaining(now)`は期限経過後に0を返すが、元の日時を変更しない。タイムゾーンなしの値・基準日時は拒否する。既存のRetry-After分類は維持し、指定なし・不正・0秒・分類時点で期限経過済みの場合は`None`、有効な未来日時だけを`RetryAt`にする。

配送側の`RedeliveryWait(message_id, retry_at)`は再配信を待たせる指示であり、SQSの操作情報や11時間上限を補完工程へ持ち込まない。Queue URLは必須設定`SQS_ARTICLE_COMPLETION_QUEUE_URL`から取得する。スライス3の補完用設定には該当項目がなかったため、スライス4で追加した。設定の欠落・型不正・空文字・空白のみは起動失敗とし、本文から配送先を選ばない。AWSへの環境変数設定と権限適用はスライス5で行う。

同期handlerはLambda contextとUTC時計を内部へ渡す。残り時間はAWSの契約を前提として`context.get_remaining_time_in_millis()`で直接取得し、メソッドの存在・callable・戻り値の型や符号を独自に再検証しない。取得時の例外は呼び出し全体へ伝播させ、無制限の代替値を置かない。資源初期化と全messageId検証後、各本文処理前に残り時間を確認し、60,000ms以上なら開始する。未満なら現在位置以降を未着手として失敗一覧へ追加する。時間不足でConsumerを呼んだり、記事・監査を変更したりしない。

再試行の失敗一覧と待機指示を別に保持し、記事処理終了後に待機を入力順で設定する。各指示で元の時刻から残り時間を計算し直し、期限経過なら省略する。残り実行時間が20,000ms未満なら残りの設定を打ち切る。対象レコードのreceiptHandleはこの段階でだけ検証し、欠落・非文字列・空白のみなら当該操作だけを省略する。有効な文字列は加工しない。

残り秒数を切り上げ、最大39,600秒で可視性変更を要求する。設定成功・通常障害・省略のいずれでもmessageIdを失敗一覧から除かない。元の時刻、要求秒数、成功時の設定秒数、上限適用を区別し、設定失敗を設定済みと記録しない。期限経過・操作情報不正・設定時間不足・記事未着手も区別する。ログには本文・receiptHandle・生ヘッダー・例外の自由文を含めず、診断障害は配送結果を変更しない。

同期SDK通信を`asyncio.to_thread`で1件ずつ実行する。Taskを保持して`asyncio.shield`で保護し、通信中にキャンセルされた場合は追加のキャンセルも含めて開始済み通信を回収してから資源を閉じ、元のキャンセルを伝播する。回収中の通常のSDK障害でキャンセルを置き換えない。開始済みの記事処理・SDK通信・資源解放への強制中断は追加しない。

根拠: [Botocore通信設定](https://docs.aws.amazon.com/botocore/latest/reference/config.html)、[Pythonのキャンセル保護](https://docs.python.org/3.13/library/asyncio-task.html#shielding-from-cancellation)。

検証は、値と境界、設定順序・時刻の再計算・省略、通信回収と診断を単体テストで確認する。実DBの配送テストでは実handler・Consumer・Collect権限のRepositoryを使い、429の時刻から可視性要求と部分応答への接続、設定失敗時の非closed状態、未着手記事・監査の維持、クライアント解放障害時の応答維持を確認する。HTTP・署名・SQS通信・時計だけを外部境界で差し替える。

スライス4の検証結果（2026-09-14）: 重要な値・handler制御・SQS遅延検証のテストを先に追加し、未実装による失敗を確認してから接続した。app全体と変更テストのRuff lint・format、全単体7,156件（`-m 'not integration'`）、`make test-integration`のDB統合1,475件、`make test-local`のローカル132件が成功した。`git diff --check`と一時DB・Redis・ネットワーク・ボリュームの削除を確認した。既存の非推奨・Logfire関連の警告は残る。AWSリソース設定・実接続と旧経路の切替は今回の対象外であり、スライス5に残す。

PR作成前の最終検証（2026-09-14）: handlerの配送判断とrecorderの診断項目選択を分離し、4工程の`SqsBatchItemIdentifier`・`SqsBatchFailureResponse`を`lambda_handlers/sqs/response.py`へ集約した。現在のコミット候補だけを取り出した一時チェックアウトで、Ruff lint・format、単体7,118件、DB統合1,475件、ローカル132件が成功した。差分チェック、一時DB・Redis・ネットワーク・ボリュームと検証用チェックアウトの削除を確認した。AWS設定・実接続は未実施で、スライス5に残す。

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
5. 別記事・過去試行の重複判定履歴を理由に、今回の記事の素材を除外しない。
6. 旧Taskiqの実行接続・失敗分類・DB確定・品質条件・宛先保護を維持する。

## 実装スライスと重要なテスト

各スライスで重要な振る舞いのテストを先に置き、実装・検証を完了してから次へ進む。型定義だけ、継承関係、内部の呼び出し順、既存分類表・品質条件を重複検証するテストは作らない。

| 順序 | 実装範囲 | そのスライスの完了条件 |
|---|---|---|
| 1 | 起動時の設定依存と資源管理 | 無関係な設定なしで起動できる。必須設定の欠如は起動失敗になる。初期化途中の失敗・終了・キャンセルで取得済み資源を解放する |
| 2 | 新経路の重複除去無効化 | 同じHTMLの再処理と、共通段落を持つ別記事の先行処理で素材が欠落しない。記事内の繰り返しを許容し、独自のキャッシュ管理を追加しない |
| 3 | 入力検証とConsumerへの接続 | 実Consumer・実DBの代表例から部分バッチ応答までを検証し、正常終了・closed確定は受信完了、再試行・個別入力不正は当該レコードの失敗となる |
| 4 | 個別待機と残り時間 | 元時刻の保持、11時間上限、待機変更失敗、残り60秒の境界、未着手分の再配信、終了処理を確認する |
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


## スライス5：Relayと補完Lambdaの配送経路（2026-09-14）

### 作業定義

- Problem: 取得が保存する`article.incomplete_recorded`をOutboxから送信する経路がなく、実装済みConsumerへAWSから届かない。
- Evidence: 取得ServiceのOutbox同時確定、共通Relayのイベント別claim・送信・published確定、既存キュー、CollectとAppのDB権限、既存Lambda・bootstrap・GitHub Actionsを確認した。ユーザーはRelay追加と旧Taskiqを維持した新経路の実動確認を選択した。
- Invariants: イベント契約、先勝ちDB確定、部分バッチ応答、60秒・20秒・11時間の制御を維持する。ConsumerはCollect、Relayは既存Outbox操作権限を持つAppとして接続する。通常applyでイメージと稼働状態を保持し、元キューの保持期間を短縮しない。
- Non-goals: DB schema・DB grant・依存・業務判断の変更、旧Taskiq停止、新規通知・アラート、他工程の配送変更。
- Done: ローカルで取得→実Relay→実Consumer→実DB確定とTerraform・CIの設定契約が通り、既存の承認経路でデプロイ後、実ログとDBで通常取得からの完成を確認する。ローカル完了とAWS完了は別々に記録する。

### 確定した設定

| 対象 | 設定 |
|---|---|
| 補完Consumer | 共通backend arm64イメージ、1024MB、600秒、予約同時実行5 |
| SQS接続 | batch 10、収集待ち0秒、最大同時実行5、`ReportBatchItemFailures` |
| 元キュー | 通常可視性3600秒、保持14日を維持、受信上限5回 |
| 補完専用DLQ | Standard、SQS管理暗号化、保持14日、元キューだけからredrive許可、TLS必須 |
| 補完Relay | 共通backend arm64イメージ、512MB、120秒、予約同時実行1、Scheduler毎分 |
| 初回のトリガー | Consumer mapping・Relay Schedulerとも無効 |

可視性3600秒は600秒の6倍で、[AWSのSQS接続推奨](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-configure.html)に対応する。同時実行は[Lambda SQS scaling](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)の制約に従い、mappingと関数の予約を5で揃える。

元キューが既に最大の14日保持であるため、既存メッセージの寿命を縮めず、DLQも14日にする。Standardキューの期限は最初の送信時刻から数えるので、DLQ移動後14日を保証しない。これは[DLQ保持期間の仕様](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)による制限である。

Consumer専用private subnetは既存の未使用区画33を使い、RDS5432、取得プロキシ、SQS endpoint443だけへ接続する。プロキシでは取得worker同様の公開記事取得を許可し、既存の非公開宛先・port保護を維持する。AI資格情報・SSM通信は追加しない。可視性変更は実行role・boundary・SQS endpoint policy・双方向SGを接続する。LambdaのSQSポーリングはLambdaサービスが担当する。

Relayはイベント所有工程の`IncompleteArticleRecordedEvent.from_input()`を再利用し、既存のOutboxエラー分類へ変換する。DBのCollectにはOutbox更新権限がないため、Relayは他工程と同じ`vector_app`を使う。DB権限は変更せず、SQS送信権限を補完キューに限定する。

CIのPassRole拒否条件は`ci-apply-pass-role`に移設し、既存のサービス限定・対象role限定を維持する。移設先の取り付け後に元policyを更新する依存関係を置く。bootstrapは全体plan・applyとし、`-target`で部分適用しない。

### デプロイと確認

1. ReadOnlyの`default`で対象環境・既存queue・DB・proxy・実行ロールを確認する。SSO未認証なら`aws sso login --profile default`をユーザーが行う。
2. `bootstrap-access/README.md`の専用`vector-bootstrap-apply`経路でbootstrap全体をplanし、追加boundary・CI権限・拒否条件の移設を先行適用する。本体CIや管理者fallbackで迂回しない。
3. PRの検証後にmainへ反映し、既存`AWS app images`でmainのbackend arm64イメージを公開する。既存のCI・Security成功条件を維持し、ECSの旧経路rolloutは行わない。
4. 既存`AWS terraform apply`へConsumer・Relayのdigestを指定する。`production`の承認後に本体を適用し、両トリガーが無効であること、設定・role・通信経路を確認する。
5. `completion_consumer_state=enabled`で受信を開始し、次に`completion_relay_state=enabled`で定期送信を開始する。無指定／`keep`はstateの稼働状態を保持する。停止は対応するstateを`disabled`にする。
6. 取得が作ったevent ID・未完成記事IDを照合し、Relayの送信、Consumerの`succeeded`、完成記事と`article.analyzable_created`確定を確認する。`not_required`だけでは新Consumerでの実取得成功と扱わない。429の可視性変更、再配信、DLQは別の確認項目として記録する。

旧Taskiqが先に完成する場合や新ConsumerとHTTP取得が重複する場合がある。これは今回の並行運用に伴うものであり、既存の先勝ち確定で扱う。

### 検証結果

コミット候補だけの一時チェックアウトでRuff lint・format、単体7,133件（`-m 'not integration'`）、`make test-local`の133件が成功した。取得が作った未完成イベントを実RelayからSQS通信境界へ送り、その実本文を実Consumerへ渡して、Collectによる完成確定と次工程Outboxまで確認した。外部境界でRDS署名・HTTP・SQS通信だけを差し替えた。

Terraformは本体26件・bootstrap14件のモックテストとfmt・validate、既存インフラスクリプト11件が成功した。workflowの実shellテストでstate取得失敗・ECRに存在しないdigestの拒否・一時設定の非配置、通常のイメージ／稼働状態保持を確認した。actionlint 1.7.12は既存の`concurrency.queue: max`を未対応として拒否し、mainの同じ行でも再現した。この既存診断だけを除外した変更workflowの検査は成功した。既存のTerraform非推奨・Logfire関連の警告は残る。

ReadOnlyでのAWS確認: 補完Consumer・補完Relayは未配置。元キューの可視性は30秒、保持14日、redrive未設定、可視／処理中メッセージはいずれも0件だった。RDS `vector-db`はavailableかつIAM認証有効、SQS Interface endpointはavailableかつprivate DNS有効、予定区画`10.0.33.0/24`は未使用だった。これは設定読取であり、補完の実通信を確認した結果ではない。

`make test-integration`のDB統合1,475件が成功した。一時DB・Redis・ネットワーク・ボリュームと検証用チェックアウトを削除し、`git diff --check`を確認した。bootstrapの専用SSOは未認証のため、実plan・適用は未実施。AWSでの送信・受信・HTTP取得・可視性変更・DLQ・DB確定ログは未確認で、スライス5のAWS完了条件に残る。
