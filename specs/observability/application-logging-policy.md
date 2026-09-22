# アプリケーションログの概念別ポリシーとCloudWatch集約

作成: 2026-09-17
更新: 2026-09-20（共通基底・AI分析の出力契約との整合）
Status: Accepted
Implementation: Partially implemented。共通基底の規則・processor・例外構造化・チェーン部品を実装済み。既存アプリへの接続、詳細な原因抽出、URLの目的別変換、CloudWatch配送の検証は未完了。

関連: [#317 記録方針の共通化](https://github.com/yook11/Vector/issues/317)、[#328 エラーの情報保持と安全な記録の分離](https://github.com/yook11/Vector/issues/328)

概念別仕様: [AI分析のログポリシー](./ai-analysis-logging-policy.md)
共通機構の正本: [アプリケーションログの共通基底ポリシー](./logging-base-policy.md)。本書は到達すべき運用契約を定め、実装済みの保証範囲・deny/allow/maskの意味・上限は基底仕様を参照する。

## Problem

例外型だけのログや例外文の一律置換により、異なる失敗原因を調査できない。一方、ログの入口ごとに出力・秘匿の判断が分かれ、標準出力や手動属性には共通の制約がない。

structlogのcontextへ処理の事実を追加し、概念ごとのポリシーで出力可能な情報へ変換してからJSON化する。通常のアプリケーションログはCloudWatch Logsへ集約し、原因説明と必要な相関情報を保持する。

## Evidence

初回確認時のHEAD: `3706a4d1e`。2026-09-20に`0ccd60b8e`でベース・AI分析・ログ設定を再確認した。コード・設定・既存テストの読解に基づき、実環境のログ配送や漏洩を実証したものではない。

| 対象 | 現状と根拠 |
| --- | --- |
| API・worker | [setup.py](../../backend/app/logfire/setup.py)がstructlogを設定し、Logfire転送後にproductionではJSONへ整形する。共通の許可項目選択はない。 |
| Lambda | [logging.py](../../backend/app/lambda_handlers/logging.py)に別のstructlog設定があり、JSONをstdoutへ出す。 |
| Assessment失敗 | [failure_recorder.py](../../backend/app/lambda_handlers/assessment/failure_recorder.py)の処理失敗ログはIDと例外型を保持するが、原因説明を渡していない。 |
| 例外の内部表現 | [AI provider errors](../../backend/app/ai_providers/errors.py)はreasonを保持しても文字列に含めない場合があり、任意引数を捨てる契約もある。出力側の変更だけで失われた情報は復元できない。 |
| Logfire例外 | [redaction.py](../../backend/app/logfire/redaction.py)と[既存テスト](../../backend/tests/logfire/test_exception_redaction.py)はmessage・stacktrace・status description等の一律置換を契約にしている。 |
| URL・自由文 | [scraper.py](../../backend/app/collection/article_completion/scraper.py)には記事URLと例外文を直接記録する経路がある。 |
| frontend | [server-log.ts](../../frontend/src/lib/observability/server-log.ts)はqueryを除去するが、渡されたfieldsを展開しており、実行時の共通ポリシーはない。 |
| 独立した出力 | [EMF](../../backend/app/cloudwatch/emf.py)は直接stdoutへJSONを書き、[scheduler](../../backend/app/queue/scheduler_entrypoint.py)には標準loggingのhandlerがある。[Agent](../../backend/app/agent/research_handoff/recall.py)等には直接のLogfireログ呼び出しがある。 |
| 配送・保持 | [ECS](../../infra/aws/ecs.tf)のawslogs設定、[Assessment Lambda](../../infra/aws/assessment_consumer.tf)のlogging_configがある。[log_retention_days](../../infra/aws/variables.tf)の既定値は30日で、実環境の有効値は未確認。 |

## Invariants

- ログ呼び出し箇所やイベントごとにAllow Listを複製せず、概念単位のポリシーを共通processorで適用する。
- 通常の例外原因文・公開記事URLを一律に隠さない。SQLパラメーター、記事本文、認証情報、内部IP等は以下の情報別規則で除く。
- context・ログ引数・例外chainのどこから来た情報にも同じ出力規則を適用する。
- ポリシー適用前のデータを出力・外部送信しない。適用後のrendererが生例外や任意オブジェクトを再び文字列化しない。
- 必要な情報の内部保持とログへの公開を分離し、再試行・停止・例外伝播・保存結果を変更しない。
- 通常ログの集約先はCloudWatch Logsとする。維持するメトリクス・トレースへログ用の許可範囲を自動的に拡張しない。
- 記録時の障害で元の業務結果を変更せず、既存の初期化時fail-fast契約も維持する。

## Non-goals

- DB schema、認証・認可、公開API shape、業務の失敗分類や再試行方針の変更。
- 新規依存の追加、CloudWatchへの直接送信SDK、独自のログ配送キューの導入。
- メトリクス・トレースの全面移行、監査DBの保存契約の変更、ブラウザーからのログ収集の新設。
- 記事本文の調査用保存先の新設、本文の著作権上の適法性の一括判定、過去ログの再加工。
- デプロイ・migration・GitHub Actionsログの規則の変更。本書は通常アプリログを対象とする。
- ログのURL整形を理由とするHTTP宛先検証・SSRF防御・DNS解決の変更。

## 1. 対象と出力先

| 経路 | 到達する状態 |
| --- | --- |
| backend API・worker・scheduler・Lambdaの通常ログ | 共通の記録処理 → JSON stdout → 実行基盤の配送 → CloudWatch Logs |
| frontendサーバーログ | 同じ情報別契約をTypeScript側で適用 → JSON出力 → 実行基盤の配送 → CloudWatch Logs |
| 直接のLogfireログ呼び出し | 通常ログの入口へ移行し、Logfireへの重複転送を終了 |
| Logfireメトリクス・トレース | 計数・相関・自動計装を維持。例外情報の扱いは出力先のポリシーで定義する |
| CloudWatch EMF | メトリクスとして維持。専用の値・dimension制約を適用し、EMF構造を汎用ログschemaへ変換しない |
| 標準logging・依存ライブラリのログ | 出力元を列挙し、専用adapterを介して共通ポリシーへ接続 |

自動計装が生成するログ相当のレコードも実装時の棚卸し対象とする。`StructlogProcessor`の除去だけでLogfireへの全ログ転送が終わったとは扱わない。通常ログはCloudWatchへ切り替える。記録契約は調査に必要な情報と禁止・mask・sanitizeの定義から決め、既存のLogfireの一律置換や例外の情報制限を前提にしない。

CloudWatchは運用者に閲覧を限定する前提とし、実装時に実際の閲覧権限・転送先を確認する。保持期間は既存設定を踏襲し、本書では変更しない。開発用consoleも同じポリシーを通した後に整形し、開発環境を秘匿の例外にしない。

AWSランタイムのシステムログやOS・proxy・DBサービス自体のログは、アプリprocessorの保証範囲に含めない。未移行のアプリ出力とともに実装時の経路表で区別する。

## 2. 概念ごとのポリシー

### 2.1 定義と適用

ポリシーの正本を記録基盤内に置き、共通規則と処理目的ごとの許可項目・値制約・変換規則を定義する。識別子は基底の`LogPolicy`を使い、bounded context名ごとに別の識別子を作らない。

- `external_content_fetch`: 記事取得・本文補完のURL、HTTP結果、抽出結果、失敗診断。目的別allowは未定義。
- `ai_inference`: 記事のAI分析の相関情報・結果・失敗診断。初期対象のCurationとAssessmentは一つの完成済みルールを使い、stage属性で区別する。項目・出所・保護方法は[AI分析ポリシー](./ai-analysis-logging-policy.md)を正本とする。
- Embedding・Agent等への適用は、対象情報と責務を確認してから定義する。AI利用という理由だけで同じポリシーを無条件に適用しない。
- `user_interaction` / `pipeline_control` / `infrastructure`: 基底に識別子を定義済み。目的別allowと適用経路は別途定義する。
- DB例外は実行中の目的ポリシーで記録し、共通のSQL例外保護を適用する。例外型だけを理由に`db`という別ポリシーへ切り替えない。

これは概念の分割基準であり、イベントごとに別ポリシーを作る指示ではない。同一概念の開始・成功・失敗ログは同じポリシーを使う。

loggerの構築時に、コードが所有する完成済みルールを結び付ける。下流はcontextに事実を追加し、ログ出力時に共通processorが適用する。ルールはlogger属性が保持し、`log_policy`はprocessorが生成する。service等の値の出所は記録側が保証し、外部payloadをcontextやkwargsへ一括展開しない。

共通規則の禁止・変換は概念側のAllowより優先する。複数ポリシーのAllowを無条件に合成して許可範囲を広げない。例えばAssessment中のDB例外は、Assessmentの文脈を保ち、共通のDB例外変換を参照する。

### 2.2 フィールド規則

| 指定 | 動作 |
| --- | --- |
| `deny` | 構造化項目をキーごと除外する。allowより優先し、ネスト内にも適用する。 |
| `allow` | トップレベルの採用項目を指定する。採用後の文字列もsanitize・maskを通す。 |
| `mask` | 文字列内の指定キー付き値を`***`へ置換する。辞書の値全体を置換する指定ではなく、allowを兼ねない。 |
| sanitize・例外/URL抽出 | 既知形式の秘密やSQL実データ等を除き、通常の原因説明などを保持する。deny/allow/maskとは別の変換処理。 |
| 未登録 | トップレベルでallowにもdenyにも該当しない項目を除外し、名前・値を出さず件数を記録する。 |

任意文字列を許可した分類コード、任意辞書を許可したcontextなどで制約を迂回しない。enumや数値の業務上の値制約、ネストの子項目は目的別の記録側が保証する。ベースのallowはトップレベルだけのため、任意辞書を渡さず宣言済みの子項目を抽出する。基底の構造・上限検査とネスト内denyはその後にも適用する。

項目が存在しない場合は捏造しない。未知キーは`_unregistered_count`だけを記録する。明示denyは基底どおり`_denied_keys`、ネスト内の除外は件数で診断し、診断自体にもサニタイズと上限を適用する。

### 2.3 共通の出力情報

| 情報 | 契約 |
| --- | --- |
| timestamp・level・logger・logger_name | 基底allow。level・timestampはチェーンが生成する。 |
| log_policy | processorがloggerのルールから生成する。基底ルール単独では付けない。 |
| service・environment | 目的別allowとし、記録側が設定層・実行基盤から取得する。基底allowには含めない。 |
| event | コードで定義された安定したイベント名。入力値や例外文を埋め込まない。イベントごとのAllow Listは持たない。 |
| request_id・message_id・event_id・trace_id等 | 該当処理で取得できる相関IDを、概念ごとに形式検証して保持する。 |
| stage・対象記事ID・provider・model | 概念のAllow Listに存在し、既存の語彙・型に適合する場合に保持する。 |
| status・原因code/reason・duration・件数 | 既存の分類と単位を保持し、既知の異なる失敗を同じunknownへ潰さない。 |
| error_class・error_message・frames・error_details・causes・exceptions | 失敗時に基底の例外変換を通して保持する。通常のcause/contextとSQL診断は基底で抽出し、SDK固有の原因構造は追加実装する。 |

既存の意味が同じフィールド名を再利用し、単なる命名統一で全呼び出し元を変更しない。各概念の詳細な項目・型の列挙は、その概念を接続する実装と同時に正本へ追加する。汎用`extra`で未定義の項目を通さない。

## 3. contextから出力まで

```text
処理入口でポリシーと相関情報を結び付ける
  → 処理中にbind等でcontextを追加する
  → ログ呼び出し時にcontextとその一件の情報を統合する
  → 例外から原因説明・分類・発生箇所を取り出す
  → 共通規則と概念ポリシーで選別・マスク・サニタイズする
  → サイズ制限とJSON整形
  → stdout
  → ECS / Lambdaの配送
  → CloudWatch Logs
```

contextへ追加するだけでは出力しない。各ログ呼び出しが、その時点の情報を使って一件を出力する。処理終了まで全ログを蓄積する仕組みは作らない。

service等はプロセス単位、request/message ID等は処理単位、例外は該当ログ一件の入力として扱う。リクエスト開始・終了、SQSバッチ内の各メッセージ、並行task、Lambdaの次回呼び出しでcontextが混在しないように設定・復元する。

本文・応答全体・env/config全体をcontextへ積まない。必要な例外は一件の記録処理へ渡せるが、contextに長期間保持しない。出力用変換は元の例外や業務データを変更しない。

## 4. 情報別の出力規則

### 4.1 例外の原因文

原因文は調査用情報として保持する。例外型だけに縮退させず、通常の説明文を一律`[redacted]`へ置換しない。隠す対象は文の形式ではなく、内包されるSQL実データ・記事本文・認証情報等である。

| 例外の情報 | 扱い |
| --- | --- |
| timeout、接続失敗、model不存在、権限不足等の説明 | 原因文を保持し、混在する認証情報・内部IP等を置換する。 |
| providerのcode・reason・HTTP status | 既存の分類・応答の構造から取り出す。応答本文全体は添付しない。 |
| アプリの検証失敗 | 検証境界が既存のEnumへ整理した`reason`と`issues(field, code)`を、`error_details.kind="application_validation"`として保持する。未知の入力キーは境界の既存分類（`event` / `payload`と`unknown_field`）を使い、ログ側で再解釈しない。 |
| 生のPydantic例外 | 件数と標準分類だけを保持する。`input` / `ctx` / `msg` / `loc` / `title`を転記せず、独自分類は`custom_error`にする。汎用的なスキーマ解釈や数値制約の自動抽出は行わない。 |
| traceback | file・function・lineを保持する。locals、ソース行、任意notes、args/repr/__dict__のdumpは添付しない。 |
| cause/context・ExceptionGroup | 原因は`causes`、グループのメンバーは`exceptions`の各要素に型・保護後の説明を保持し、同じ規則を適用する。取得元の関係ラベルは付けず、全枝で総数と深さを共有し、現在の経路の循環を検出する。 |

既知のSDK・DB・検証例外は、構造化された情報を優先して抽出する。汎用例外の通常の原因文もサニタイズ対象とし、未知の例外型という理由だけで全文を消さない。

自由文の未知形式に混入した秘密や本文を正規表現だけで完全に見分ける保証は置かない。入力・応答を含むことが分かるが安全に分離できない部分は省略し、固定の省略理由を付け、他の原因情報・型・frame・相関情報は残す。新しい例外形式はその概念の変換と回帰ケースへ追加する。

### 4.2 SQL・DB例外

- SQLパラメーターの値は、位置引数・名前付き引数・まとめ実行を問わず出さない。構造化項目はdeny、原因文への混入はSQL例外抽出で除く。
- SQLSTATE、DB操作種別、schema/table/column/constraint名と保護後の原因文は残す。
- 基底は例外に含まれるSQL本文を除去する。今回のAI分析もSQLテンプレートを許可せず、操作名・SQLSTATE・制約名・保護後の原因文で調査する。
- driverのprimary messageへ入力値が引用される場合も対象にする。`DETAIL`、`HINT`、`CONTEXT`、internal query、行データは基底どおり除去する。
- SQLAlchemyの`hide_parameters=True`は維持するが、それだけでdriverの原因文やSQL本文が保護されるとは扱わない。
- 外側の例外を保護しても、cause・別属性・手動文字列化でパラメーターが再掲されないことを確認する。

### 4.3 記事本文・入力本文

記事本文は通常の調査ログへ出さない。生HTML、抽出本文、翻訳本文、本文を含むprompt・応答・検証inputも同じ扱いとし、先頭N文字の抜粋やdebugレベルを例外にしない。

記事ID・公開記事URL・本文長・処理結果等を残し、本文が必要なときは既存の保存先または取得元を、その閲覧権限の範囲で確認する。ログ本文に代わる新しい全文保存機能は追加しない。元データが残らず再現できない場合も、本文ログを自動的に有効化しない。

この除外はログ運用上の採用方針であり、「記事本文のログ保存が常に著作権侵害になる」という法的判断を理由にしない。

### 4.4 外部記事URL・内部IP

初期対象は外部記事のHTTP(S) URLとする。通常の公開host・path・記事識別用queryは残し、URLやqueryを一律に削除しない。

- 非公開IP、loopback、link-local、IPv6の内部アドレス等を含む宛先はURL全体を`[redacted]`へ置換する。IPv4-mapped IPv6は埋め込みIPv4の分類に従う。`localhost`と設定・出所から内部と判明しているhostも同様とする。
- IPの分類基準は[既存の非公開IP方針](../../backend/app/http/destination_policy.py)と[レンジの正本](../../backend/app/http/non_public_ranges.json)を参照する。ログ側で別のCIDR一覧を作らない。
- ログ出力のためにDNS解決や通信を行わない。任意の独自ドメインが内部へ解決されることまで、ログの文字列処理で検出済みとは扱わない。
- URL内のuserinfo、認証query、署名付きURLなどの資格情報は共通規則で保護する。署名付きURL等の認証用複合値は全体を置換する。
- 認識した個人情報・非公開共有token等も公開記事URLとして素通ししない。通常の公開記事URLと異なる用途には、その概念の規則を追加する。
- 解釈が曖昧なURLや不正なエンコードで安全に処理できない場合は、原文にfallbackせず当該URLを省略する。
- 内部IP等はURL属性だけでなく、例外文へ同じ値が現れた場合も保護する。生値を含む省略理由を出さない。

ここでの「弾く」はログへの出力を除く意味であり、記事取得の可否やHTTP通信の宛先方針を変更するものではない。

### 4.5 共通の認証情報・出力整形

password、API key、Authorization、session token、認証cookie、秘密鍵、接続URLの資格情報を生で出さない。既知の機微フィールドは値の長さにかかわらず全体を除去・マスクし、既知形式の検出は補助として使う。

基底の構造・予算検査を先に行い、採用する文字列へsanitize・maskを適用する。上限超過は基底の固定マーカーまたは固定イベントへ置換し、生文字列の先頭抜粋は作らない。改行・制御文字等でログ行を偽装できないJSON出力とする。多数の原因を抽出する場合は、出力側だけでなく抽出側でも有限の上限と循環検知を設ける。

アプリの検証診断は`AnalyzableEventInvalidError` / `IncompleteArticleEventInvalidError` / `CuratedEventInvalidError` / `AssessedEventInvalidError`の4種類に対応する。既定の共通入口`convert_exception`がSQL・Pydantic・イベント検証・共通アプリケーション例外・通常例外を種類別の変換へ振り分ける。各変換は同じ`ConvertedException`を返し、固有の診断構造は各担当側が所有する。業務分類・再送出方法は維持し、実行環境への接続は別作業とする。値準備の例外用上限は19で、例外探索の深さ8にある`issues`の`field`・`code`まで保持する。

## 5. 出力例

以下は期待する表示の合成例であり、実際の障害ログではない。追加項目はイベント別ではなく、該当概念のポリシーに登録する。

公開記事URLを保持し、失敗の説明を読める例:

```json
{"timestamp":"2026-09-17T00:00:00Z","level":"warning","service":"collection","environment":"test","log_policy":"external_content_fetch","event":"content_parse_error","article_id":123,"url":"https://example.com/news/article?id=42","error_class":"RuntimeError","error_message":"Article extraction failed: unsupported document structure"}
```

DBの原因を残し、パラメーターと行データを出さない例:

```json
{"timestamp":"2026-09-17T00:00:00Z","level":"error","log_policy":"ai_inference","event":"assessment_message_failed","stage":"assessment","operation":"save_result","error_class":"sqlalchemy.exc.IntegrityError","error_message":"duplicate key value violates unique constraint","error_details":{"kind":"postgresql","sqlstate":"23505","constraint_name":"example_unique_constraint"}}
```

`http://127.0.0.1/private`を含む失敗では、URL属性を`[redacted]`とし、原因文の同じURLも置換する。timeout等の説明や例外型は保持する。記事本文が同時に渡されても出力しない。

## 6. ポリシー未定義・記録失敗

未登録フィールドは省略する。ルール省略時は`BASE_LOG_RULES`を使い、基底allowと共通保護だけを適用する。不正なルール型はfactoryで拒否する。processor内の失敗は`log_policy_failed`と固定の`_policy_error`へ置換し、元データや未取得のmetadataを補わない。

未登録のイベント名ごとにAllow Listを要求する仕組みは作らない。イベント名はコード由来という契約を入口とテストで保証する。

既知の不正値や局所上限は基底の固定マーカー等へ置換し、共有予算超過はログ全体を固定イベントへ置換する。予期しないprocessorの失敗も原文へ戻さない。renderer・stdoutの失敗は出力境界で元の業務結果を維持し、同じloggerへ再帰して診断しない。processorだけで出力先の障害を保護できるとは扱わない。

## 7. Implementation

共通部品は[基底仕様](./logging-base-policy.md)の範囲で実装済み。2026-09-20の更新は文書のみであり、既存ログ・テスト・AWS設定・Issueを変更していない。以下は未完了の接続・拡張工程である。

1. 既存の共通部品を使い、目的別allow・追加deny/maskと、不足する原因連鎖・検証診断・URL変換を実装して保持と非露出を検証する。
2. Assessment Lambdaの処理失敗と検証失敗を先行接続する。既存のreasonを渡し、通常の原因説明・field/code・相関情報が出ることを確認する。
3. API・残りのLambda・稼働中worker/schedulerを接続する。処理入口でポリシーを選び、個別のマスク処理を共通処理へ寄せる。
4. frontend・標準logging・直接出力・直接Logfire呼び出しを棚卸しして接続する。Logfire通常ログ転送を除去し、メトリクス・トレースを回帰確認する。
5. stdoutの実出力と、許可された検証環境でのCloudWatch到達・検索可能性を確認する。未移行経路と適用済み経路を一覧にする。

#328の例外側の整理では、ログ出力を理由に原因情報を捨てる制約を外し、原因文・元例外・失敗箇所・実行条件の不足を発生元で補う。`SAFE_ATTRS`等の既存実装を新しい記録契約の根拠にせず、内部で情報を保持する責任と出力時に保護する責任を分ける。通常ログはLogfireからCloudWatchへ切り替え、出力時に新しいポリシーを適用する。

PythonとTypeScriptで情報別契約と合成入出力例を共有する。仕様を共有するためだけの新規コード生成・設定配信機構は導入しない。

## 8. Verification

### 受入ケース

| ID | 条件 | 期待結果 |
| --- | --- | --- |
| P01 | 同じ概念の複数イベントを出力 | 同じポリシーが適用され、イベントごとの項目設定が不要。 |
| P02 | 同じ値をbind・contextvars・ログ引数から渡す | すべて同じ許可・変換結果となる。 |
| P03 | 未登録フィールド・ネスト・入力由来の未知キー | トップレベルは基底、ネストは目的別の明示抽出で除外し、許可された診断項目は残る。 |
| P04 | 外部入力でlog_policy・service等を上書きしようとする | logger所有のルールと、記録側が設定したmetadataを変更できない。 |
| E01 | 同じprovider例外型でtimeoutとconnectionを発生 | reasonと原因説明で区別でき、型だけにならない。 |
| E02 | 通常の原因文と秘密値を含む例外 | 原因の説明を残し、秘密値だけを保護する。 |
| E03 | SQL位置引数・名前付き引数・まとめ実行 | 各独立ケースで値が出ず、SQLSTATE・原因・制約は残る。 |
| E04 | DB primary message・DETAIL・causeに入力値 | 生値が再掲されず、分離できた原因説明は残る。 |
| E05 | 検証失敗に本文・未知field・値を含むmsg/ctx | 本文等は出ず、宣言済みfieldと違反内容を識別できる。 |
| E06 | chain・ExceptionGroup・循環・locals・notes | 原因関係とframeは残り、生データなしで有限時間に終了。 |
| U01 | 公開記事URLにpath・通常の記事識別query | URLを保持し、過剰にqueryを削除しない。 |
| U02 | 非公開IPv4・IPv6・mapped IPv6・localhost・既知内部host | 独立ケースでURL属性と原因文の内部宛先を保護する。 |
| U03 | URLにuserinfo・token・署名、または不正な構造 | 認証値を出さず、不正形式で原文にfallbackしない。 |
| U04 | 外部記事URLのログ出力 | DNS・HTTP通信を行わず、既存の取得動作を変えない。 |
| B01 | 本文が属性・ネスト・prompt・例外inputに混入 | 全経路で本文が出ず、記事ID・URL等が残る。 |
| C01 | 並行要求・バッチ内メッセージ・Lambda再利用 | ID・policy・その他contextが別処理へ混ざらない。 |
| F01 | ポリシー不正・sanitizer失敗・renderer失敗 | 原文のfallbackなし。安全な最小診断と業務結果を維持。 |
| F02 | 長文・制御文字・切り詰め境界に秘密 | JSON一件として読め、秘密の断片や偽のログ行が残らない。 |
| O01 | production stdoutと開発consoleの実出力 | 秘匿内容が一致し、JSON/表示の整形で原文が復活しない。 |
| O02 | Logfire通常ログ転送の撤去 | 通常ログはCloudWatch経路へ出て、新しいポリシーで原因情報を保持・保護する。metric/spanの計数・相関は維持。 |
| O03 | frontend・標準logging・EMF・自動計装 | 接続済み経路の保証と専用契約を確認し、未移行を明示。 |
| O04 | CloudWatchへ配送した合成ログ | event・相関ID・原因で検索でき、非露出を実際の保存結果でも確認。 |

共通processorの単体テストだけで完了にしない。各プロセスの設定を通した最終stdout、標準loggingのhandler、Logfireの送信結果など、実際の出力境界を検証する。異なる条件・期待結果のケースを一関数へ押し込めない。

既存の[Logfire設定テスト](../../backend/tests/test_logfire_setup.py)、[例外redactionテスト](../../backend/tests/logfire/test_exception_redaction.py)、[Lambda設定テスト](../../backend/tests/lambda_handlers/test_logging.py)、[frontendログテスト](../../frontend/src/lib/observability/server-log.node.test.ts)を責務に合わせて更新・維持する。Logfire側の一律置換の既存期待を、CloudWatch側の原因文を消す根拠として流用しない。

本書作成時はコード・テストの読解と文書整合性の確認のみ。テスト実行、実ログ取得、AWS設定変更、配送検証は未実施。実装時は`/check`を実行し、未検証項目は理由と追跡先を明記する。

## Done

- [ ] 共通規則と概念ごとのポリシーが一箇所の定義で管理され、処理入口から出力まで適用される。
- [ ] 例外の原因文・公開記事URL・相関情報を保持し、SQLパラメーター・記事本文・認証情報・内部IP等の非出力を確認できる。
- [ ] Assessment失敗・検証失敗で具体的な理由を調査でき、例外型だけの記録から改善される。
- [ ] 通常ログのCloudWatch集約とLogfire通常ログ転送の撤去を確認し、メトリクス・トレース・監査の既存契約を維持する。
- [ ] 出力経路の接続、context分離、異常時動作を受入ケースで検証し、未対応経路を明記する。
- [ ] 実装リンク・検証結果・Statusを更新する。文書作成だけでImplementedにしない。

## 一次資料

- [structlog Bound Loggers](https://www.structlog.org/en/stable/bound-loggers.html): context統合とprocessor chain。
- [structlog Context Variables](https://www.structlog.org/en/stable/contextvars.html): contextの管理と実行方式による分離。
- [Logfire Scrubbing](https://pydantic.dev/docs/logfire/instrument/scrubbing/): scrubberの適用範囲と本文・URL等の注意点。
- [SQLAlchemy Hiding Parameters](https://docs.sqlalchemy.org/en/20/core/engines.html#hiding-parameters): SQLパラメーター表示の抑制。
- [Pydantic Error Handling](https://pydantic.dev/docs/validation/latest/errors/errors/): 検証エラーのinput・ctx等。
- [ECSからCloudWatchへのログ配送](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html): stdout/stderrとawslogs。

外部資料は機構の根拠であり、概念の分割・本文除外・内部IP除外は本アプリで採用した設計判断である。


## 例外からログ専用基底を撤去（2026-09-21）

`VectorDomainError`と`SAFE_ATTRS`は全利用先から撤去する。DB、SQS送信、取得工程・投入、投入Lambda、Outboxの各基底も通常の`Exception`を直接継承する。DBの独自`__str__`も廃止するが、基底の直接生成禁止・必須reasonの型検証・原因チェーンは維持する。

既存コンストラクターの引数と保持属性を維持し、メッセージ未指定時は`args == ()`、`str(error) == ""`とする。独自コンストラクターを持たない基底は標準の引数保持に従う。型名・code・reason・件数・固定文による補完は行わない。監査の文字列変換結果が空の場合は既存の処理で`payload.error_message=null`とし、既存の構造化コード・分類・例外型・原因チェーンを維持する。DBスキーマ・既存行は変更しない。

この撤去では新しいメッセージ引数、原因情報の追加取得、ログ変換・出力ポリシーの接続は行わず、既存のLogfire出力保護と公開応答の境界を維持する。
