# ArticleCompletionConsumer — 未完成記事の補完をイベント駆動へ移行する

Status: 共通HTTP・補完固有エラーの定義はPR #356で実装・マージ済み（2026-09-13）。共通HTTPの変換、新経路用HTTP取得・HTML抽出・記事の統合と構築を実装済み。新経路の失敗分類とRetry-After解釈を実装済み。正常終了型・失敗後処理・ConsumerとDB確定処理を実装済み。記事取得工程の共通起動・資源管理と補完への配線を実装・検証済み。配送ハンドラー・AWS接続は未実装。配送の確定設定と後続スライスは[配送仕様](./article-completion-delivery.md)を参照する。HTTP起因の判断とRetry-After解釈は2026-09-26に外部取得の失敗判断へ切り出した。

## Problem

未完成記事の保存を契機として、HTML取得・本文抽出・記事の完成をOutbox → SQS → Lambdaで実行する。再試行の主体をSQSへ移し、完成した記事をキュレーションのイベント経路へつなぐ。

イベント駆動化の大枠と、段階的に実装した境界を定義する。確定した失敗分類と配送設定を記録し、実装済みの境界と後続の検討事項を区別する。

## Evidence

- [取得イベント](../../backend/app/collection/article_acquisition/events.py)と[取得サービス](../../backend/app/collection/article_acquisition/service.py): 未完成記事の保存と同時に`article.incomplete_recorded`をOutboxへ記録する。現在のpayloadは`source_id`と`incomplete_article_id`。
- [補完タスク](../../backend/app/queue/tasks/completion.py)と[Ready](../../backend/app/collection/article_completion/ready.py): 定期処理が対象を`running`へ変更して投入し、Readyもその状態を要求する。再投入と期限切れleaseの回復は定期処理が担当する。
- [Repository](../../backend/app/collection/article_completion/repository.py)と[Service](../../backend/app/collection/article_completion/service.py): 試行番号と`running`条件で更新を限定し、完成記事・未完成行の削除・成功監査・Outboxを同じトランザクションで確定する。
- [失敗処理](../../backend/app/collection/article_completion/failure_handling.py)と[再試行定義](../../backend/app/collection/article_completion/retry_policy.py): 現行は失敗理由に応じて`open`への差戻し・次回時刻設定、または`closed`への遷移を行う。
- [失敗分類](../../backend/app/collection/article_completion/scrape_failure.py): 通信の失敗原因と本文抽出の失敗を保持するが、再試行方針は現行DBスケジュールに結び付いている。
- [補完ポリシー](../../backend/app/collection/sources/article_completion_policy.py)と[Completer](../../backend/app/collection/article_completion/completer.py): ソース別に観測値とHTMLからの値を採用し、完成記事の品質条件を検証する。
- [保存テスト](../../backend/tests/collection/article_completion/test_service.py)と[Repositoryテスト](../../backend/tests/collection/article_completion/test_repository.py): 保存競合、古い試行の更新拒否、Outbox失敗時のロールバック等の証拠。
- [キュレーション仕様](./curation-consumer.md)、[Assessment仕様](./assessment-consumer.md)、[Embedding仕様](./embedding-consumer.md): 配送、Consumer、資源管理、監査、部分バッチ応答の参照元。

既存コードは現状の証拠であり、移行後の仕様を自動的に決めるものではない。稼働中のAWS環境は本整理では確認していない。

## 合意した方針

1. 再試行はSQSに寄せる。新経路の再投入の主体を旧DBポーリングにしない。
2. 重複配送を前提とし、同じ記事へのHTTP取得が複数回実行されることを許容する。
3. 競合は先にDBの状態遷移を確定した処理が勝つ。成功結果を失敗結果より優先する仕組みや、HTTP取得の重複を防ぐ新しい実行権は追加しない。
4. 補完できなかった場合は、理由に応じて再配信か受信完了を決める。理由ごとの詳細は本書の確定した失敗判断に従う。
5. DLQとDBを別の責務として扱い、DLQへの移動に伴うDB更新を追加しない。DBには補完を続けてよいかを残し、再試行の回数上限だけを理由に`closed`にしない。
6. 切替方法は重複時の現行挙動に基づいて具体化する。重複の存在だけを理由に、旧処理をすべて消化する手順を必須にしない。
7. 救済の再投入は、未完成行のうち`closed`ではないものをSQSへ送る。
8. 受信時にも対象行が存在し、`closed`ではないことを確認する。行がない、または`closed`ならHTTP取得を行わず受信完了にする。
9. 再試行する失敗では行を残し、`closed`にせず、SQSの再配信に任せる。再試行する意味のない失敗では`closed`への更新をcommitしてから受信完了にする。
10. 補完成功では、完成記事の保存・未完成行の削除・成功監査・後続Outboxを同じトランザクションでcommitしてから受信完了にする。

## 全体フローと引き継ぐ責務

```text
未完成記事の保存 + article.incomplete_recordedをOutboxへ記録
  → relay → 補完用SQS ← closedではない未完成行の救済再投入
  → Lambda → 補完Consumer → DBの対象行を確認
      ├─ 行なし / closed: HTTP取得を行わず受信完了
      └─ 行あり・closedではない: 入力構築 → HTML取得・抽出 → 記事を完成
          ├─ 成功: 完成記事・未完成行削除・成功監査・Outboxをcommit → 受信完了
          │    → 記事完成イベントからキュレーションのイベント経路
          ├─ 競合で先に完了・closed確定: 上書きせず受信完了
          ├─ 再試行する失敗: 行を残しclosedにせずSQS再配信
          └─ 再試行する意味のない失敗: closedへの更新をcommit → 受信完了
```

- イベントの入口には既存の未完成記事保存イベントを使う。記事内容・URL・補完ポリシーはDBと既存の宣言から取得する。
- 完成時はキュレーション移行で定義する共通の記事完成イベントへ接続する。補完ConsumerからキュレーションのTaskiqタスクを直接投入しない。
- Lambda入口はSQS形式、部分バッチ応答、接続・資源の生成終了を担当する。Consumer・ServiceにはSQSのreceipt handle等を持ち込まない。
- 重複除去以外のHTML抽出設定、フィールドの採用ルール、完成記事の品質条件、外部通信の既存の保護を維持する。品質判定は観測値との統合後に行う。外部通信中にDB接続・トランザクションを保持しない。
- 完成記事の保存・未完成行の削除・成功監査・Outboxは同じトランザクションで確定し、失敗した場合はロールバックする。commit成功後に受信完了とし、競合した処理は後続イベントを追加しない。
- 失敗理由・元の原因を保持し、監査の分類とSQSへの応答判断を分ける。受信完了を記事完成成功と混同しない。

### Consumer接続時の戻り値と失敗監査

Consumerは未完成記事IDを受け取り、完成・追加処理不要・補完失敗を区別する。補完失敗は例外を投げ直す代わりに、元例外と工程の判断結果を一緒に返す。再試行の結果を返すことは、SQSの受信完了を意味しない。

- 再試行する失敗は、行を閉じずに元例外と待機時刻を返す。失敗監査の保存障害で元の結果を置き換えない。
- 終了する失敗はclosed確定後に失敗結果を返す。失敗監査はclosedの確定後に行い、監査の障害で確定済みclosedを再試行へ戻さない。closedの確定自体が失敗した場合はDB障害の再試行結果を返す。
- 同じURLの完成記事が別経路ですでに保存されていた場合は、既存記事を維持して対応する未完成行を削除し、追加処理不要とする。新たな成功監査やOutboxは追加しない。

接続の保証は[補完ローカルテスト](../../backend/local_tests/README.md#completionのconsumer接続テストファースト)に先行して定義する。Consumer本体を接続し、先行17ケースに初回DB照会障害と外部キャンセルの2ケースを加えた19ケースで検証する。

## 競合の受け入れ条件

現行の「先に確定した処理が勝つ」という方針を引き継ぎ、新経路では対象行の存在と`closed`ではないことを受信時・確定時の条件にする。以下は移行後の契約であり、新たな並行実行テストの実施結果ではない。

| 先に確定した処理 | 後続の処理 | 許容する結果 |
|---|---|---|
| 完成記事を保存し、未完成行を削除 | 完成記事の保存 | 後続は保存を進めず、後続イベントを追加しない |
| 完成記事を保存し、未完成行を削除 | 失敗状態への更新 | 対象なしで更新せず、完成結果を維持する |
| 恒久失敗で`closed`へ変更 | 完成記事の保存 | 後続の保存は成立せず、`closed`を維持する |
| 恒久失敗で`closed`へ変更 | 失敗状態への更新 | 後続は更新せず、先に確定した状態を維持する |

受信時の確認だけでは競合を防げないため、完成保存に伴う未完成行の削除と`closed`更新も「対象行が存在し、`closed`ではない」を条件に原子的に行う。条件が成立せず、先行処理による削除・`closed`確定が分かった場合は上書きせず受信完了にする。具体的なSQL・ロック方法は実装時に定義する。

後から成功した処理を優先する変更は要求しない。先行トランザクションがロールバックした場合は、確定した勝者として扱わない。再試行する失敗は行を残して補完可能な状態を維持するため、それだけで並行処理の完成保存を拒否しない。

## 救済・受信・確定の契約

| 場面 | DB・処理 | SQSへの応答 |
|---|---|---|
| 救済の再投入 | `closed`ではない未完成行を選び、SQSへ送る | 受信処理ではない |
| 受信時に行がない、または`closed` | HTTP取得・補完を行わない | 受信完了 |
| 受信時に行があり、`closed`ではない | DBの事実から入力を構築して補完へ進む | 実行結果に応じて決める |
| 再試行する失敗 | 行を残し、`closed`にしない | 再配信対象 |
| 再試行する意味のない失敗 | `closed`への更新をcommitする | commit後に受信完了 |
| 補完成功 | 完成記事・未完成行削除・成功監査・後続Outboxを同時にcommitする | commit後に受信完了 |
| 確定時に先行処理による削除・`closed`確定が判明 | 上書きせず、後続イベントを追加しない | 受信完了 |

- 救済対象の選択後に記事が完成・`closed`になり得るため、通常投入・救済投入のどちらも受信時の確認を通す。
- DB照会の失敗を「行がない」と扱わない。完成保存や`closed`更新のcommitに失敗した場合も受信完了にせず、再配信対象にする。
- 再試行する失敗の処理から、並行処理で確定した`closed`を再開したり、削除された未完成行を作り直したりしない。
- DLQへ移動しても未完成行を`closed`にしないため、その行は救済対象に残る。救済は30分間隔のEventBridge Schedulerがbackfill Lambdaを起動し、`closed`でない未完成行を新しいevent_idで再投入する。`now - 7日 <= created_at < now - 30分`を古い順に一回50件まで送り、7日を超えた行は`closed`にする。

## 再試行とDBの境界

- 再配信対象はSQSへ失敗として返し、受信完了対象は部分バッチ失敗一覧へ含めない。回数上限・DLQへの移動はSQSの設定が担当する。
- 原因別の分類は、通信、HTTP応答、取得ポリシー、抽出結果、抽出処理の異常、完成記事の入力条件、DB、期限切れ、想定外例外を確認する。最初のスライスの契約案を次節に記載し、未合意の分類を実装済み・確定済みとして扱わない。
- 「抽出器の異常」はHTMLから本文を取り出す処理の例外・想定外戻り値を指す。Outbox relayの障害ではない。旧経路の分類は維持し、新経路では調査対象として再試行する。
- `Retry-After`の個別待機は配送側で最大11時間とする。通常可視性とDLQの設定は[配送仕様](./article-completion-delivery.md)のAWS接続スライスに従って具体化する。
- DBの状態や保存内容をDLQに合わせて再設計しない。SQS受信回数をDBの試行番号と同一視せず、旧DB側の試行上限による`closed`化を新経路では使わない。
- 現行のReady・保存にある`running`と試行番号の要求は、新経路では「対象行が存在し、`closed`ではない」という条件に合わせる。`open`・`running`のどちらも対象に含め、旧leaseの取得や`ready_at`の到来を新経路の実行条件にしない。
- 上記の合意に必要な受信・更新条件と失敗処理を変更する。既存カラムの削除やDB schema変更は本方針に含めず、具体的な実装は既存のDB制約との整合性を確認する。

## 最初のスライス: 正常終了・失敗理由の契約案

### Problem・参照元

新経路では、問題なく終えた結果と、発生事実・原因を伝えるエラーを区別する。新しいエラーは既存Taskiqのエラー変換・ハンドラーには接続せず、新しいイベント駆動経路で使う。DB更新条件やSQS接続の実装は後続スライスとする。

[CurationCompletion](../../backend/app/analysis/curation/service.py)、[AssessmentCompletion](../../backend/app/analysis/assessment/service.py)、[EmbeddingCompletion](../../backend/app/analysis/embedding/service.py)の「保存完了・処理済みを正常終了に含める」考え方を参照する。エラーは[CurationError](../../backend/app/analysis/curation/errors.py)と同様に失敗理由と元の原因を保持する。

### 正常終了と失敗の境界

- 完成、処理済み、競合で先行処理を採用した結果は正常終了とする。補完拒否・取得や保存の実行障害は失敗として扱う。
- 正常終了は「今回完成した」と「追加処理が不要だった」を区別する。型は既存AIと同様に不変の結果値とし、完成時だけ新規保存した正の`analyzable_article_id`を持つ案とする。
- 行なし、既に`closed`、先行処理との競合、完成記事のURL競合は追加処理不要として終了できるが、その理由を区別する。行なしから完成済みを推測したり、今回完成していない記事の成功監査・Outboxを生成したりしない。
- 今回の補完拒否は失敗であり、`closed`をcommitしてSQSへ受信完了を返しても正常な記事完成へ分類し直さない。後の重複受信で既存の`closed`を確認する場合は、追加処理不要として終了する。
- 正常終了・失敗と、SQSの受信完了・再配信は別の軸とする。失敗をすべてSQS再配信へ送る契約にはしない。

### エラーと工程の責務

エラーは「何が起きたか」を伝え、`retryable`・再試行方針・工程が決めた再試行時刻を持たない。補完工程のハンドラーがエラーを受け取り、再試行可否・待機時間・`closed`化を判断する。

エラーのフィールドと意味はエラー側の契約とし、ログ基盤に依存せず、工程名を付けるためだけに共通HTTPエラーを補完エラーで包み直さない。工程ハンドラーが工程情報を補い、ログ・監査・メトリクス等の出力側が項目と形式を明示的に選択する。全フィールド・文字列表現・原因例外をそのまま送信する前提にしない。

通信失敗と確認できて詳細不明な場合は共通の通信分類`UNKNOWN`とする。通信失敗か不明な通常例外や既知の宛先拒否はHTTP変換の対象外とし、元のまま工程ハンドラーへ伝える。最終的に未分類の通常例外は工程側で扱い、キャンセル・プロセス終了は通常失敗へ変換しない。

共通HTTPの変換関数はエラーを返すだけとし、後続の呼び出し側が元の例外を`raise ... from ...`でつなぐ。応答受信時刻はHTTP実行側が応答受信直後・ステータス検証前にタイムゾーン付きUTCで記録し、status・生の`Retry-After`と共に変換へ渡す。変換側は現在時刻を取得せず、待機指示の欠如・空値・日時・不正値も解釈しない。実際の時刻取得・例外チェーン・工程と出力への接続は後続タスクで検証する。

既存処理を残したまま新経路を独立して実装し、完成・切替後に旧処理を削除する。

HTTPの通信失敗・エラー応答は`app/http/`の全工程共通エラーとし、補完専用に定義し直さない。抽出失敗・補完拒否は補完工程が所有する。独立した定義タスクと後続の接続順は[エラー契約の実装プラン](../../plans/pipeline/article-completion-error-contract.md)を参照する。

| 情報 | 意味・制約 |
|---|---|
| 失敗理由 | アクセス拒否、回数制限、通信障害、抽出結果なし等の安定した理由コード。既存の原因コード・defectを再利用する |
| 相手からの待機指示（任意） | Retry-Afterを相手が返した事実として保持する。採用するか、いつ再試行するかは工程側で決める |
| 元の原因 | HTTP status、共通の`HttpTransportFailure`、抽出defect、原因例外等 |

再試行判断は本文補完のハンドラーで決め、Lambdaの配送処理でstatusから再計算しない。DB障害・想定外例外を補完拒否へ丸めず、元の原因を保持して失敗を伝える。

本文・HTML断片・ヘッダー全体・秘密情報をこの契約に追加しない。既存のエラーが持つ自由文を、そのまま配送診断へ出す契約にはしない。

### 補完工程の確定した失敗判断

新経路の`classify_completion_failure(exc, *, now)`は、補完工程の対処を決める純粋関数である。エラーそのものやHTTP規格に再試行方針を持たせない。HTTP起因の失敗は[外部取得の失敗判断](../collection/external-fetch-failure-classification.md)の結果を補完の対処へ写し、それ以外を補完工程として判断する。

| 原因 | 対処 | 調査対象・補足 |
|---|---|---|
| HttpResponseError、HttpTransportError、HostBlockedError | 外部取得の失敗判断に従う | 再試行可能なら再試行、再試行不可なら終了。code・retry_at・調査対象は判断の値をそのまま使う |
| FetchDeadlineExceededError | 再試行 | 取得全体の期限切れ |
| RobotsDisallowedError | 終了 | robotsルールによる明示的な拒否 |
| ResponseSizeLimitExceededError、ArticleContentTypeError | 終了 | 現在の取得・抽出条件では扱えない |
| ArticleExtractionEmptyError、ArticleContentQualityError | 終了 | 抽出結果なし・品質不足。品質判定の新規接続は行わない |
| 既知の理由だけのArticleCompletionRejectedError | 終了 | 完成記事の条件不足 |
| ArticleExtractionCrashedError、未分類の構築拒否、その他の通常例外 | 再試行 | 調査対象。DB例外等の専用分類は今回追加しない |

- 構築拒否は`UNMAPPED_VALIDATION_ERROR`を含むか`unmapped`が非空なら、既知の理由が混在していても再試行を優先する。`defects`が空の場合も調査対象として再試行する。
- robotsの404は取得処理で記事取得へ進むため、この分類へ届かない。robotsの403はHTTP拒否であり、robotsルールによる明示的禁止に変換しない。
- 戻り値は`RetryArticleCompletion | CloseArticleCompletion`を直接記述し、外側の型やユニオンの別名は作らない。どちらも不変で`code`と`requires_investigation`を持ち、再試行の結果だけが`retry_at`（`RetryAt | None`）を持つ。対処は結果型で区別し、actionフィールドは持たない。HTTP起因の失敗は外部取得の失敗判断の`code`、補完固有のエラーは既存CODE、分類対象外は`unknown`とする。
- 元例外・原因チェーン・構築拒否の詳細を変更せず、呼び出し側が保持する。調査対象は即時通知の指示ではなく、出力や緊急度の判断は後続処理が所有する。
- キャンセル・プロセス終了は対象外とし、呼び出し側は通常のExceptionだけを渡す。正常な処理済み・競合・対象行なし・closed済みは扱わない。
- DB更新・SQS操作・監査・ログ・通知はこの関数内で行わず、旧Taskiqには接続しない。
- DLQへ移った記事も、非closedであれば救済によって新しいメッセージとして再投入してよい。人の対応まで停止する要件はなく、再投入抑制は追加しない。DLQの受信回数制限は記事全体の累積試行上限ではない。

HTTP応答の判断の根拠は[外部取得の失敗判断](../collection/external-fetch-failure-classification.md)に置く。旧Taskiqの分類は置き換えない。

ボット拒否は取得可否の判断であり、回避処理は含めない。200で返るチャレンジ画面等を判定する専用機構は現行にはないため、根拠なくボット拒否という理由を付けない。専用検出を追加するかは別途判断し、現行の抽出失敗と区別する根拠がある場合だけ専用理由を使う。

### Retry-Afterの確定した解釈

Retry-Afterの解釈は[外部取得の失敗判断](../collection/external-fetch-failure-classification.md#retry-afterの解釈)へ移した（2026-09-26）。補完工程は判断が返した`retry_at`をそのまま`RetryArticleCompletion`へ渡す。配送側は元のretry_atを保持したまま個別待機を最大11時間へ制限し、長い指示より早い再試行を許容する。設定失敗を尊重済みとは扱わない。

### 最初のスライスの完了条件

正常終了と失敗を区別し、エラーが発生事実・相手の待機指示・元の原因を伝え、工程の再試行判断を持たない契約を定義する。エラー定義だけの段階ではテストを追加せず、変換処理・ハンドラーを実装する段階で振る舞いを検証する。クラス構成や属性の持ち方だけを固定するテストは作らない。DB状態遷移・Consumer・SQS接続は後続で実装する。

## SQS・Lambdaへの配送接続

確定した契約と実装スライスは[記事補完のSQS・Lambda配送仕様](./article-completion-delivery.md)を正本とする。Lambdaは最大10分、最大10件を逐次処理し、残り60秒未満なら次の記事を開始せず未着手分を再配信対象とする。1件全体の期限は追加しない。

再試行は部分バッチ失敗一覧で返し、有効なretry_atがあればreceiptHandleを使って可視性を変更する。元のretry_atを維持し、実際の個別待機を最大11時間へ制限する。待機変更が失敗しても受信完了にしない。配送診断はCloudWatch向け構造化ログへ出し、DB監査・記事の状態判断と分ける。

起動時の不要な設定依存を解消し、抽出結果が別記事・過去試行のキャッシュに依存しないようにする。起動と資源管理、抽出の独立、入力とConsumer接続、待機と残り時間、AWS接続の順に実装・検証する。救済と旧経路の切替は別途扱う。

## 切替時に具体化する事項

- 先勝ちの保存・失敗遷移を新旧経路でも維持できることを確認し、重複実行を許容する前提で切替手順を決める。
- 旧poller・期限切れlease回復・投入済みタスクが新経路のDB状態へ及ぼす作用を整理し、再試行の主体をSQSへ移す。
- 保存済みOutboxイベントの扱いと救済の投入手順を決める。救済の対象条件は`closed`ではない未完成行とし、`open`・`running`を含める。`closed`の再開は行わない。
- 通常経路の切替と、過去データの救済や不要カラムの整理を分ける。旧処理の完全消化は必須条件として固定しない。

## Invariants

1. 完成記事の保存・未完成行の削除・成功監査・Outboxは同時に確定し、そのcommit後に受信完了にする。
2. 重複配送・HTTP取得を許容し、先に確定した完成・`closed`を後続の競合処理が上書きしない。
3. 再配信と受信完了の判断は失敗理由に基づき、詳細は実装時に根拠とともに定義する。
4. DLQへの移動からDB状態を自動更新しない。
5. 新経路の再試行はSQSが担当し、旧DBポーリングとの二重管理を完成形にしない。
6. 再配信対象を待たせるために受信完了へ変換せず、外部通信・待機中にDBトランザクションを保持しない。
7. 救済は`closed`ではない未完成行をSQSへ送り、受信時にも行の存在と`closed`ではないことを確認する。
8. 再試行する失敗では行を残し、`closed`にしない。再試行する意味のない失敗では`closed`更新のcommit後に受信完了にする。
9. 受信時の確認に加え、完成保存・`closed`更新も対象行の存在と`closed`ではないことを原子的な条件とする。DB照会・確定の失敗を受信完了に変換しない。

## HTML抽出と素材の契約

取得済みの`RawResponse`から同期関数`extract_html_content()`で`ScrapedContent`を返す。HTTP取得・再試行・保存は担当せず、旧Taskiqからは呼び出さない。

- `RawResponse`と`ScrapedContent`は補完パッケージ内の`content.py`で共有し、旧`scraper.py`経由のimportも維持する。`content_type`は欠如を`None`で表せる。
- 入力はパラメーターを除去し、前後空白・大文字小文字を正規化したメディアタイプが`text/html`の応答に限る。欠如・JSON・PDF・XHTML等は元の値を保持した`ArticleContentTypeError`とする。
- HTTP charset指定があれば`decoded_text`を使う。指定がなければ先頭2,048バイトのHTML内charset指定を試し、指定なし・デコード失敗時は`decoded_text`へ戻る。新しい抽出処理自体はログを出力しない。
- Trafilaturaの精度優先・コメント除外・表抽出・メタデータ・日時抽出設定は維持する。新経路の重複除去は無効にし、別記事・過去試行の履歴を理由に素材を除外しない。記事内の文章の繰り返しは許容し、独自のキャッシュ管理は追加しない。戻り値と例外は[公式の抽出API](https://trafilatura.readthedocs.io/en/latest/corefunctions.html#bare-extraction)に沿って境界で扱う。
- 抽出結果`None`は`ArticleExtractionEmptyError`、Document以外は`ArticleExtractionCrashedError(UNEXPECTED_RESULT)`とする。抽出器が投げた通常例外だけを`ArticleExtractionCrashedError(EXCEPTION)`へ変換し、元例外をチェーンする。周辺処理の想定外例外・キャンセル・プロセス終了はそのまま伝播する。
- `ScrapedContent`は不変の素材であり、タイトル・公開日時の欠如を許容する。本文欠如は空文字にする。`from_extraction()`でタイトルのタグ除去・entity変換・前後空白除去・500文字への切り詰め、本文の前後空白除去、既存の日時変換を行い、空タイトルは`None`にする。
- タイトル必須・本文50文字以上等は素材の生成時に判定しない。ソース別の採用方針で観測値と統合した後、既存の`AnalyzableArticle.build_or_reject()`で完成条件を判定する。`ArticleContentQualityError`はこの抽出処理では使わない。
- 共有素材の変更に伴い、旧スクレイパーも短い本文・タイトル欠落で品質不足の早期終了をせず、統合・構築へ進む。旧経路の失敗記録も抽出品質不足から構築拒否へ移り得る。旧エラー型・ハンドラーは残し、新しい抽出エラーを旧経路へ接続しない。

ソース別にHTML補完を通す必要があるか、JSON/APIで補完すべきかの整理はイベント駆動化後に行う。工程側の判断とService・Consumerへの接続は後続タスクとする。

## 記事の統合・構築の契約

`html_completion.py`の同期関数`complete_with_html(observed, completion_policy, html, *, source_id, source_url)`は、既存の観測値と`ScrapedContent`を受け取り、完成した`AnalyzableArticle`を返す。DBの状態や試行回数を持つ`ReadyForArticleCompletion`には依存しない。対象行の存在・非closedの確認は後続の呼び出し側が担当する。

- 既存の`ArticleCompletionPolicy.resolve()`で値を統合し、`AnalyzableArticle.build_or_reject()`で構築する。値の優先順位・欠如時の扱い・完成条件をこの関数で再定義しない。
- `QualityTooLow`だけを`ArticleCompletionRejectedError`へ変換して投げる。`defects`と`unmapped`の順序・内容・重複をそのまま保持する。`unmapped`の既定値は空tupleとする。
- `UNMAPPED_VALIDATION_ERROR`は分類できなかった検証結果として保持し、処理中の想定外例外と混同しない。構築結果は例外ではないため、原因例外や疑似的な例外チェーンを生成しない。
- 統合・構築で投げられたその他の例外は捕捉せず伝播する。新関数と新エラーは再試行判断・ログ出力を追加しない。既存の構築処理内にある未分類検証のログは維持する。
- 旧`completer.py`の処理は呼ばず、旧Taskiqの戻り値・接続は維持する。HTTP・DB・SQS操作、監査・保存・再試行判断と新経路への接続は後続タスクとする。

## 新経路のHTTP取得契約

`article_fetch.py`の`fetch_article_response(url: SafeUrl) -> RawResponse`は、robots確認後に記事を非同期で取得する。呼び出しごとに既存の外部HTTPクライアントを生成し、両通信で共有して終了時に閉じる。既存User-Agent・プロキシ・宛先保護を使用し、内部リトライ・リダイレクト追従・robotsキャッシュを追加しない。旧Taskiqの取得処理と新経路の接続は変更しない。

- robots URLは記事と同じscheme・host・portの`/robots.txt`とする。2xxは既存`RobotFileParser`で解析し、空本文は空のルールとして扱う。404は本文を読まず記事取得へ進む。それ以外の非成功応答・通信失敗・取得期限超過では記事を取得しない。
- robotsの明示的な禁止は`RobotsDisallowedError`で伝える。robotsの403等はルールによる禁止と区別し、記事の非成功応答と同じく共通`HttpResponseError`で伝える。
- robots・記事とも10MiBを上限とし、上限ちょうどは受け入れる。ステータス判定後、解釈できる非負整数のContent-Lengthが超過していれば本文を読まず拒否する。欠如・不正値・過小申告時も、64KiB単位で渡される圧縮展開後の本文を累積確認し、超過チャンクを保持せず受信を中断する。
- サイズ超過は`ResponseSizeLimitExceededError`が、`resource`（`ROBOTS_TXT` / `ARTICLE_PAGE`）・上限・確認したサイズ・`size_basis`（`DECLARED_CONTENT_LENGTH` / `RECEIVED_DECODED_BODY`）を保持する。`resource`は原因ではなく取得していたもの、`size_basis`は申告値か受信・展開後の実測値かという判定の根拠を表す。中断時のサイズを最終的な全体サイズとは扱わず、本文上限をSDKの一時展開を含むプロセス全体の厳密なメモリ上限とはしない。
- 通信待ち時間と取得全体の期限は、robotsが10秒、記事が30秒とする。記事の期限はrobots確認後に開始する。`asyncio.timeout()`で接続準備・応答待ち・本文受信を囲み、自身の期限切れだけを`FetchDeadlineExceededError`（取得していた`resource`・制限秒数）へ変換する。期限で受信を中断して資源を解放するが、後始末を含む厳密な実時間上限とはしない。ルール解析・HTML抽出・記事構築・DB保存は取得期限に含めない。
- HTTPXの通信失敗は既存変換を使って`HttpTransportError`へ渡し、元例外をチェーンする。ヘッダー受信直後・ステータス検証前にUTC時刻を記録し、HTTP応答のstatus・生のRetry-Afterを保持する。HostBlockedError・対象外例外・外部キャンセルはそのまま伝播する。
- 受信成功時は元のContent-TypeとHTTP charset指定、圧縮展開済みの本文を保持し、HTTPXが選んだ文字コードと置換処理でデコードする。HTML内charsetとContent-Typeの受け入れ判定は、既存のHTML抽出処理が担当する。

取得エラーは`CODE`と発生事実だけを持ち、再試行判断・ログ出力・本文断片を持たない。抽出・構築との接続、工程判断、監査・DB・SQS・Consumerへの接続は後続タスクとする。

## Non-goals

- ソースからの記事取得工程のイベント駆動化。
- 本文抽出アルゴリズム、品質基準、ソース別の値の採用ルールの改善。
- 成功優先の競合解決や、HTTP取得の重複を防ぐ新しい排他制御。
- DLQとDB状態の同期、DB schema・保存モデルの再設計。
- Retry-AfterのSQS接続、実行時間・同時実行数・保持期間の本整理での実装。
- 過去記事の一括救済、旧Taskiq全体の撤去、SQS・Lambda・AWS接続の実装とデプロイ。

## Done

本整理は、合意した方針、参照できる既存契約、実装時に具体化する事項をこの文書で区別できれば完了とする。

後続実装では、初回受信から記事完成イベントまでの接続、救済対象と受信時の再確認、再配信・commit後の受信完了、先勝ちの競合、DB照会・commit失敗時の再配信、切替対象の扱いを具体化し、単体・実DB・SQS/Lambda接続に適した検証を行う。細部の未決定を埋めるためだけに今回コードを変更しない。


## ConsumerとDB確定の実装境界

`ArticleCompletionConsumer(session_factory).consume(incomplete_article_id)`は、既存のcaller管理セッションを受け取り、`CompletionSucceeded | CompletionNotRequired | CompletionFailed`を返す。結果型は不変で、追加処理不要はmissing・closed・superseded・url_conflictの理由を保持する。失敗は元のExceptionと`RetryArticleCompletion | CloseArticleCompletion`を保持し、受信完了・SQS再配信を直接実行しない。

新経路専用RepositoryはID・状態・ソース情報・URL・観測値だけを読み、試行番号とleaseを実行条件にしない。行なし・closedはHTTPも監査も実行しない。同URLの完成記事が見つかった場合は非closedの未完成行だけを削除・commitし、url_conflictとして既存結果を採用する。入力構築と取得・抽出・構築は読取セッションを閉じてから行う。

保存時はIDと非closedを条件に未完成行をDELETEし、削除できなければsupersededとする。削除後に既存の完成記事RepositoryでINSERTし、新規保存の場合だけ成功監査（article_completed）と記事完成Outboxを追加して一括commitする。URL競合は未完成行の削除だけを確定する。終了も同じ非closed条件のUPDATEでclosed・leased_until=None・updated_atを設定し、更新できなければsupersededとする。旧Ready・Service・試行番号付きRepositoryの実行処理は呼び出さない。

失敗ハンドラーがUTC現在時刻を既存分類へ渡す。再試行では行の状態・ready_at・lease・attempt_countを変更しない。closed確定の障害は既存のセッション境界でDB例外へ変換してから再分類し、DB障害の再試行結果を返す。失敗監査は別トランザクションとし、その二次障害で元例外・待機時刻・確定済みclosedを変更しない。通常Exceptionだけを扱い、外部キャンセルを失敗結果へ変換しない。

監査は既存CompletionPayloadを使い、再試行をfailed、終了をrejected、outcome_codeを判断code、retryabilityを工程判断に合わせる。対象ID・既知のソース情報・例外型と原因チェーンの型名・HTTPステータス・定義された通信／抽出理由・構築defectsを明示的に選ぶ。自由文・本文・生のヘッダーを追加出力せず、未分類の構築詳細と待機／調査情報は元例外と結果値に保持する。監査schema、エラー定義、失敗分類表は変更しない。

今回の完了条件は19ケースを含むローカルテスト全体、単体、DB統合、lint・formatの成功と一時環境の削除である。配送ハンドラーの結果変換、Retry-Afterの可視性制御、救済投入、旧経路との切替・デプロイは後続タスクとする。Consumer実装時点では抽出器のプロセス内キャッシュの寿命を変更していない。配送スライス2では、重複除去を無効にして別記事・過去試行に依存しない抽出とし、補完ローカルテストのキャッシュリセットも不要にする。


## 処理結果の計測（2026-09-23）

Problem: 補完の新経路は`processing_outcome`を送出せず、completionの処理結果は旧Taskiq経路の送出だけで記録されている。旧経路を撤去すると成功・失敗の件数を確認できなくなる。

Evidence: `record_completion_processing_outcome`を呼ぶのは旧経路のService・失敗処理・タスクだけである。他の3工程はConsumerの成功確定点と失敗処理で送出している。

- Consumerの1回の処理につき、成功はsucceeded、失敗はfailedを1件送出し、処理不要は送出しない。
- 成功は完成記事・未完成行削除・成功監査・Outboxのcommit後に数え、commitの失敗はfailedとする。
- 失敗は再試行・終了の判断や原因によらずfailedとし、新経路ではinfra_errorを使わない。終了はclosedの確定後に数え、supersededになった場合は数えない。
- 記事ではなく処理の回数を数える。再配信・救済による同じ記事の処理もそれぞれ数えるため、再試行が続く記事があると成功率は実態より低く見える。
- 失敗率のアラームは設けない。失敗の大半は外部サイトの拒否で、率が上がっても取れる対処がない。成功率はメトリクスで確認し、原因はログの`completion_message_processed`と監査で追う。

Invariants: 失敗処理での計測の障害は、監査と同じく元の例外・待機時刻・確定済みclosedを変えない。メトリクス名・dimension・値は変更しない。

Non-goals: 旧経路の撤去とinfra_errorの語彙からの削除、失敗理由・判断のdimension追加、Consumerの停止検知。

Done: 各結果の送出件数とresultをローカルテストで確認し、completionの失敗率アラームを削除する。デプロイ後にConsumerのログでprocessing_outcomeの送出を確認する。

検証結果（2026-09-23）: 送出を追加する前に新規ローカルテスト12件を実行し、記録する側の7件が送出なしで失敗することを確認してから実装した。実装後、変更ファイルのRuff lint・format、`tests/collection/`・`tests/cloudwatch/`の単体1,141件、補完のローカルテスト45件が成功し、ローカルテスト全体の収集（317件）も確認した。Terraformは隔離コピーでfmt・init（backend無効、lockfile readonly）・validate・test（38件）が成功した。DB統合テストはRepository・SQLを変更していないため実行していない。本番への反映と送出の確認は未実施。

本番確認（2026-09-25）: 反映後、Consumerのログでfailedの送出を確認した。succeededは確認待ち。


## 旧Taskiq経路の撤去（2026-09-25）

Problem: 補完は新経路（Outbox→SQS→Consumer）と救済で完結しているが、旧Taskiqの定期処理（`dispatch_html_fetch_jobs`・`sweep_expired_leases`）と`scrape_html_body`が並走し、同じ未完成行を先に確定している。旧経路を撤去し、補完の実行主体を新経路と救済だけにする。

Evidence: 新Consumerは本番で成功・closedを確定し、処理結果も送出している。救済は30分間隔で稼働し、`vector_backfill`の権限と再投入のIAM・VPC endpoint・キューポリシーを確認した。手動取得も未完成行の保存と同じ取引で補完イベントをOutboxへ書く。

- 旧Taskiqの3タスク、補完Streamの購読と監視、旧Ready・Service・試行番号付きRepository・旧スクレイパー・旧失敗分類と、その監査・メトリクス・テストを削除する。
- 新Consumerの結果型`CompletionSucceeded`は`consumer_result.py`へ移す。
- 記事完成のドメイン不変条件とソース別の昇格確認は、新経路の`complete_with_html`で検証する。
- `processing_outcome`の語彙から`infra_error`を、Logfireから`vector.completion.lease_swept`を外す。
- 撤去時に`running`で残る行は、closedでない行として救済が再投入し、新経路で処理する。

Invariants: 新経路・救済・手動取得の動作、未完成行のDB状態、リース列とCHECK制約、監査payloadのschemaは変更しない。

Non-goals: リース列・`running`状態の整理（migrationを伴う別PR）、Valkeyの補完StreamとACLの削除、手動取得の新契約、救済の再送条件の変更。

Done: 旧経路への参照がなく、単体・DB統合・補完と救済のローカルテストが通る。反映後、旧経路の定期処理が止まり、救済が再投入した行をConsumerが処理したことを確認する。

検証結果（2026-09-25）: Ruff lint・format、単体7,147件、`make test-integration`（`tests/queue`・`tests/collection`・`tests/audit`）78件、ローカルテスト（completion・backfill・acquisition）90件が成功し、ローカルテスト全体の収集（329件）も確認した。旧経路の監査投影と2 Stream構成のコメントを固定していたテストは、対象とともに削除した。本番への反映と、救済の再投入の確認は未実施。
