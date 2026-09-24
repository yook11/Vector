# AI分析のログポリシー

作成: 2026-09-17
更新: 2026-09-22（AssessmentのLambda共通ログ設定への依存を解消）
Status: Accepted
Implementation: Partially implemented。Assessmentの入口・終端、初期化・バッチ不正・共有ライフサイクルのcleanupをAI目的ポリシーへ接続済み。Consumer・Service・失敗後処理も同じメッセージ用ロガーへ接続済みで、Repositoryの重複ログは例外記録へ集約した。AssessmentのAI呼び出しと共有DeepSeekクライアントのcleanupも接続済み。通知処理内部とSSM cleanupは記事分析とは別の目的ポリシーへ接続済み（[上位仕様§2.4](./application-logging-policy.md#24-キャッシュ更新通知秘密情報取得2026-09-22)）。Curation・Embedding・エージェントの業務ログへの適用は未移行。HTTP・AI SDK例外の入力値保護は未対応。追加診断・保護要件、AWS適用とCloudWatch到達確認は後続工程とする。

上位仕様: [アプリケーションログの概念別ポリシーとCloudWatch集約](./application-logging-policy.md)
基底の正本: [アプリケーションログの共通基底ポリシー](./logging-base-policy.md)
関連: [#317 記録方針の共通化](https://github.com/yook11/Vector/issues/317)、[#328 エラーの情報保持と安全な記録の分離](https://github.com/yook11/Vector/issues/328)

変更状況: mask・sanitize と文字列内の認証情報の置換は[ログの情報漏洩防止と項目別サニタイズの責務分離](./logging-leak-prevention-policy.md)を優先する。本書で mask を文字列内のキー付き値の置換、sanitize を既知形式の秘密の検出とする記述と、文字列中の本文を mask で伏せる記述は旧契約。

## Problem

AI分析の失敗ログに例外型しか残らず、初期化・入力構築・通信・応答解析・保存のどこで何が起きたか調査できない。分析結果の全文を見ることよりも、失敗の原因、発生箇所、実行条件、対処を追えることを優先する。

正常時は実行の事実を簡潔に、失敗時は診断情報を詳しく記録する。AssessmentとCurationを同じAI分析ポリシーで扱い、工程は属性として区別する。

今回見直す直接の理由は、実際の調査で失敗理由がログに残っていなかったことである。禁止情報とその保護方法を先に定義し、その境界内で調査に必要な情報を積極的に残す。既存ログに項目がないことや、現在のベースが抽出しないことを、その情報を記録しない理由にはしない。

## AIプロバイダー・Assessment例外の診断契約（2026-09-22）

- `AIProviderError`と`AssessmentError`は、ログ基盤に依存しない`ApplicationError`を継承する。既存の具体型、`CODE` / `code`、`reason`・`provider_error`・`defect`の保持と組み合わせ検証を維持する。
- 説明は空文字の維持を目的とせず、失敗を分類・検知した場所で固定文を渡す。例として「AIプロバイダーとの通信がタイムアウトしました」「AI応答のcategoryが文字列ではありません」を使い、SDKの生メッセージ、応答本文、不正な実値は埋め込まない。
- プロバイダー例外は任意の文字列`message`と任意の`reason: StrEnum | None`を受け取る。説明を省略した呼び出しでは、具体型で確定する失敗種別の固定文を使う。任意オブジェクトや複数の位置引数を説明として受け取らない。
- DeepSeek・GeminiのSDK例外分類とAssessmentの応答検証では、理由に対応する具体的な説明を渡す。Assessmentの工程例外は任意のキーワード`message`を受け取り、省略時は工程の失敗理由を説明する固定文を使う。プロバイダー例外の任意メッセージを工程の説明へ転記しない。
- `details`はプロバイダーでは取得できた`code` / `reason`、Assessmentでは`code` / `reason`とする。応答不正の具体的な`defect`は既存の`code`に反映されるため重複登録しない。プロバイダー例外本体やSDK属性、応答の任意属性は診断辞書へコピーしない。CODEのない基底型ではcodeを補完せず、診断項目がなければ`details=None`とする。
- 既存の`convert_exception` → `convert_application_error`が説明と診断を共通形式へ写す。専用のAI変換分岐は追加しない。呼び出し側の分類済み判定は既存の具体型10種類とそのサブクラスを維持する。
- メッセージが付くことで、既存の監査経路でも従来空だった説明が保存され得る。監査スキーマ・分類・通知判断は変更しない。Curation・Embeddingの工程例外の継承と説明は今回変更しない。
- 未定義のcategoryは`AssessmentResponseInvalidError`の説明とcodeで診断を完結させる。入力値を含む元の`ValueError`は`raise ... from None`で原因表示を抑制し、共通ログではアプリケーション例外の説明・code・reason・発生frameを残す。Pythonの`__context__`から例外オブジェクト自体を消去する処理ではない。
- 共通の原因抽出と通常のValueErrorの変換は変更しない。SDK例外の説明には応答由来の値が入り得るため、専用変換による保護は後続とする。この診断契約変更に続くAI呼び出し・クライアントのロガー接続は§3.3.3に記載する。

2026-09-22の診断契約変更では、AIプロバイダー・Assessment・共通例外変換・handler・監査payloadの対象単体テスト833件が成功した（DB統合83件は選択対象外）。変更したPython 13ファイルのRuff lint・format確認も成功した。SDK由来の値を新しい外側の説明へ転記しないことと、共通変換が説明・code・reasonを取得することを確認した。全体テスト・DB統合テスト・原因連鎖の保護変更・ロガー接続・デプロイは実施していない。

続くcategory原因表示の抑制では、parse・defect契約・アプリケーション例外変換・JSON出力の対象テスト97件が成功した。実際のparseで生成した例外を共通processorとrendererへ渡し、未定義のcategory値が原因経由でも出力されず、説明・分類・発生位置が残ることを確認した。SDK例外の保護は未対応のままとする。

## Evidence

定義時に確認したHEAD: `c3e595b11`。以下の表は2026-09-21時点の記録であり、実環境の出力検証ではない。2026-09-22の接続範囲はImplementationを参照する。

| 対象 | 確認した事実 |
| --- | --- |
| Lambdaの失敗記録 | [Assessment](../../backend/app/lambda_handlers/assessment/failure_recorder.py)・[Curation](../../backend/app/lambda_handlers/curation/failure_recorder.py)の処理失敗ログはIDと例外型を記録し、原因文やstackを渡していない。 |
| 処理期限 | [Assessment Consumer](../../backend/app/analysis/assessment/consumer.py)・[Curation Consumer](../../backend/app/analysis/curation/consumer.py)は業務処理全体を60秒に制限し、失敗後処理はその期限の外で行う。 |
| 原因の分類 | [Assessment分類](../../backend/app/analysis/assessment/consumer_failure_classification.py)・[Curation分類](../../backend/app/analysis/curation/consumer_failure_classification.py)はproviderの詳細reasonを`failure_reason`へ投影する。 |
| 内部例外 | [Assessment errors](../../backend/app/analysis/assessment/errors.py)・[Curation errors](../../backend/app/analysis/curation/errors.py)は`provider_error`と`code`を属性で保持する。文字列は標準の空文字となり、診断情報は属性・原因チェーンから取得する。 |
| 通信の原因 | [Gemini translator](../../backend/app/ai_providers/gemini/error_translator.py)はtimeoutとconnection等を区別する。ログ側で独自の分類を作り直す必要はない。 |
| 検証失敗 | [Assessment parse](../../backend/app/analysis/assessment/ai/parse.py)には欠落・型違反・値違反等のdefectがあり、未知の検証失敗を元の例外として伝える経路もある。 |
| 正常結果 | [Assessment Service](../../backend/app/analysis/assessment/service.py)には`in_scope`・`out_of_scope`・`already_assessed`、[Curation Service](../../backend/app/analysis/curation/service.py)にはsignal・noise・処理済みの区別がある。 |
| 対処と二次障害 | [Assessment handler](../../backend/app/lambda_handlers/assessment/handler.py)は失敗項目をバッチ応答へ含める。[失敗後処理](../../backend/app/analysis/assessment/consumer_failure_handling.py)は監査・計測・通知の障害を元の失敗と区別しているが、診断は主に例外型である。 |
| ベースとAIルール | [base.py](../../backend/app/log_policy/base.py)の基本allowは5項目。[ai_inference.py](../../backend/app/log_policy/policies/ai_inference.py)にはモデル・入出力トークン数と§3.3.1の処理情報を定義済み。実行経路には未接続。 |
| 例外抽出 | [exceptions/extraction.py](../../backend/app/log_policy/exceptions/extraction.py)は例外型・原因文・frame・原因連鎖・ExceptionGroupを抽出し、SQL診断と、[イベント検証変換](../../backend/app/log_policy/exceptions/event_validation.py)による4種類の検証例外のreason/issuesを利用できる。今回、既存の変換は変更しない。provider属性等の追加診断は後続工程。 |
| 接続と使用量 | [Lambdaログ設定](../../backend/app/lambda_handlers/logging.py)はポリシー未接続。[DeepSeek](../../backend/app/analysis/assessment/ai/deepseek.py)の一部失敗ログに`completion_tokens`があるが、正常系の入出力使用量や全工程の経過時間は記録していない。 |

## Invariants

- 共通規則の上に一つのAI分析ポリシーを置く。イベントやAssessment/CurationごとにAllow Listを複製しない。
- 原因説明、発生箇所、原因のつながり、既存の詳細reasonを保持し、すべてを例外型やunknownだけへ縮退させない。
- 情報を許可することと保護することを両立させる。allowに登録した文字列もsanitize・maskを通し、禁止部分だけを除いた診断を残す。
- 診断可能なプロダクションコードの失敗は、AI provider由来かどうかにかかわらず対象にする。
- 記事本文・分析結果本文・prompt・応答全体・SQLパラメーター・認証情報は出さない。例外に混入した場合も同じ規則を適用する。
- 記録するのは取得できた事実と実際の判断であり、発生箇所・再試行・使用量等を推測しない。
- 記録処理は業務結果、例外伝播、再配信、transaction、監査保存の契約を変更しない。
- CloudWatch向けログの許可項目を、Logfireのspanやmetricへ無条件に追加しない。

## Non-goals

- 分析結果の内容や品質をログから閲覧・評価する機能、本文の別保存先の新設。
- Agentのユーザー質問、週次分析、Embedding等への一括適用。初期対象は記事のAssessmentとCurationとし、追加適用時に必要情報を確認する。
- 再試行戦略・timeout設定・例外分類・業務結果の変更、新しい計測サービスや依存の導入。
- 今回の定義変更でのhandler・structlog設定・例外変換・SQS応答・監査・メトリクスの変更、AWS操作、Issue更新。

## 1. ポリシーの単位と責任

識別子は既存の`LogPolicy.AI_INFERENCE`に揃え、出力を`log_policy=ai_inference`とする。`analysis`という別識別子や入力用の`policy`フィールドは新設しない。各loggerは完成済みの同じAI分析ルールを構築時に保持し、`stage=assessment`または`stage=curation`で工程を区別する。初期化、実行、失敗後処理、cleanupまで同じポリシーを適用する。

`stage`は業務工程、`operation`はその中で実行していた処理を表す。現在初期化ログの`stage`へ渡している`settings`等は、接続時に`operation`として扱い、同じ名前に別の意味を持たせない。

| 担当 | 責任 |
| --- | --- |
| 処理入口・実行側 | ポリシーと相関情報を設定し、実行中のoperation・時間・利用設定を記録側へ渡す。 |
| 例外の生成・変換側 | 原因の意味と必要な事実を保持する。ログ表示の都合で原因を捨てない。 |
| 既存の分類・handler | 失敗分類と業務上の対処を決定する。 |
| AI分析側の記録処理 | 既存分類・SDK・schemaから本書の項目だけを抽出し、値の意味・型・ネスト形状を保証する。 |
| 共通の記録処理 | 基底の例外抽出・構造検査・sanitize・mask・上限を適用し、完成済みルールで選別してJSON化する。 |
| 実行基盤 | stdoutをCloudWatch Logsへ配送する。 |

Assessment中のDB例外も`ai_inference`の文脈で記録し、DB例外の抽出・秘匿規則は基底の共通処理を使う。DBエラーという理由で相関情報を捨てたり、別ポリシーの許可項目を丸ごと合成したりしない。

認証情報のdeny・maskは基底を継承し、本文・応答などの禁止はAI推論側で定義する。denyは構造化項目の除外、maskは文字列内の指定キーに対応する値の置換、sanitizeは既知の秘密形式の検出・置換を担う。denyからmaskへの暗黙継承はないため、文字列でも保護する禁止キーは両方に明示する。`extend(allow=...)`は親の目的別allowを引き継がないため、モデル・使用量を含む本書の許可項目を一つの完成済みルールにまとめる。

## 2. 調査対象となる失敗

| 発生場面 | 残すべき診断 |
| --- | --- |
| 設定・クライアント・Consumer等の構築 | 失敗した構築処理、設定項目名、例外文、stack。設定値全体は出さない。 |
| 対象データの読み込み・入力構築 | 対象ID、取得・構築のどちらか、必要な前提の欠落、型・検証上の不整合。 |
| AI通信 | provider/model、HTTP statusやprovider code、timeout/connection等のreason、保護後の原因文。 |
| 応答解析・検証 | JSON解析・構造検証・業務上の構築のどこか、失敗field・defect・期待する型や制約。 |
| 結果保存・commit | 保存とcommitのどちらか、SQLSTATE、制約名、DBの原因説明、保存結果が確定しているか。 |
| 想定外のコード不具合 | TypeError、AttributeError、assertion等の例外型・原因文・呼び出しstack。既知の業務例外だけに限定しない。 |
| 監査・通知・metric・cleanup | 元の失敗との関係、二次障害のoperation・原因・stack、業務結果への実際の影響。 |

同じ例外型でも別の原因を識別できることを要件とする。例えばtimeoutとconnection、認証失敗と権限不足、入力の欠落と型違反を区別する。既存分類にない事実を無理に既知codeへ当てはめない。

## 3. 許可項目と値の契約

### 3.1 共通の扱い

以下をAI分析の完成済みルールと記録側の値契約とする。全項目を毎回埋めるのではなく、取得できたものを記録する。必要な情報が現在伝達されていない箇所はImplementationの不足として扱い、取得・伝達を追加する。

- 基底allowは`event` / `level` / `timestamp` / `logger` / `logger_name`の5項目。`log_policy`はprocessorがルールから生成する。下表の追加項目を基底へ無条件に加えない。
- allowは出力項目の選択であり、サニタイズ免除ではない。すべての文字列と例外由来項目に共通のsanitize・目的別mask・上限を適用する。
- ベースのallow選別はトップレベルだけである。下表の構造化項目はAI分析側で宣言した子項目だけを新しいdict/listへ抽出する。任意の`extra` / `metadata`、SDK応答や例外の丸ごとdumpは許可しない。
- 未取得は項目を省略し、0・1・空文字・推測した分類で補わない。取得済みの0は残す。数値ではboolを拒否し、時間は有限の非負数、件数は非負整数とする。
- 調査に必要な項目の不足は本書へ追加し、同じAI分析ルールに反映する。正常経路の未登録項目を黙って受け入れず、最終出力の契約テストで検出する。

### 3.2 出力項目

| 項目名 | 出所・値の契約 |
| --- | --- |
| `service`, `environment` | 実行側の固定識別子・設定層の環境名。AI用追加allow。入力payloadからコピーしない。 |
| `stage` | `assessment` / `curation`。初期化とcleanupも工程を維持する。 |
| `operation` | §3.3のコード所有の処理名。失敗を記録している場所ではなく、失敗が発生した処理を示す。 |
| `request_id`, `message_id`, `event_id`, `trace_id`, `span_id` | Lambda context、SQSの検証済み識別子、検証済みイベント、tracer由来。任意payload中の同名項目は採用しない。 |
| `analyzable_article_id`, `curation_id`, `analyzed_article_id`, `noise_id` | 取得・保存結果から得た正の整数。読み込み前でも検証済みイベントで確定したIDは記録できる。 |
| `provider`, `model`, `prompt_version` | 使用中のadapter/spec由来の識別子。`model_name`属性をログの`model`へ対応付ける。 |
| `outcome` | 正常終端の`CompletionKind.value`。Assessmentでは`in_scope` / `out_of_scope` / `already_assessed`。前提不成立や失敗の説明文を正常結果へ混ぜない。 |
| `rejection_code` | ReadyBuildで確定した既存reasonの値。Assessmentの前提不成立は失敗ログへ記録し、再処理を要求しない判断は`message_disposition=completed`で別に示す。 |
| `reason`, `field`, `record_index` | SQS・イベント入力不正の既存reason、宣言されたfield、0始まりのレコード位置。Assessmentの保存見送りでは固定値`reason=concurrent_write`、DeepSeek応答の打ち切りでは`reason=output_token_limit_reached`も許可する。正常終端の結果は`outcome`、前提不成立は`rejection_code`を使い、例外自由文には再利用しない。 |
| `code`, `failure_reason` | 既存の例外から取得する。providerの詳細reasonや応答defectを汎用の失敗codeに潰さない。 |
| `failure_kind`, `retryability` | DB障害など既存の非provider分類に値がある場合のみ記録する。providerの回復分類は廃止し、代替分類やunknownで埋めない。 |
| `http_status`, `provider_code`, `finish_reason` | 既知SDKの対応属性から取得するstatus/code/終了理由。statusは100〜599の整数。codeは整数または128文字以内の英数字・`_` / `.` / `:` / `-`からなる識別子、終了理由はAssessmentでは§3.3.3の既知値だけを許可する。status/codeは形式が有効なら新しい値も残し、既存分類へ無理に対応付けない。status/codeの形式外の説明文は保護後の`error_message`へ残す要件とし、SDK専用変換は後続で実装する。 |
| `error_class`, `error_message`, `frames` | `exc_info`から基底が抽出する外側の例外情報。DeepSeek cleanupでは`exc_info`を渡さず、`error_class`だけを例外型の完全修飾名から明示する。`frames`は`file` / `function` / `line`のみ。外側の原因文が短いcodeでも内側の診断を省略する理由にしない。 |
| `causes` | 原因の構造化リスト。各要素は例外情報、取得済みの`code` / `failure_reason` / `http_status` / `provider_code` / `error_details`、子の`causes` / `exceptions`のみ。外側と同じ例外構造を使い、取得元を示す`relation`ラベルは付けない。各例外を同じ保護経路に通す。 |
| `exceptions` | ExceptionGroupのメンバー。各要素は外側と同じ例外構造を持ち、原因と同じ総数予算を使う。上限で残りを省略した場合は末尾に`[limit]`を置く。 |
| `error_details` | 型別診断。PostgreSQLは`kind: "postgresql"`と取得できた`sqlstate` / `schema_name` / `table_name` / `column_name` / `constraint_name` / `data_type_name`のみ。SQLSTATEは英大文字・数字5文字。アプリ用変換を指定した検証例外は`kind: "application_validation"`と既存の`reason` / `issues(field, code)`を持つ。診断は共通sanitize・目的別mask・上限を通し、SQL診断属性はパラメータの部分一致置換から独立させる。トップレベルの`sqlstate` / `constraint_name`は出さない。 |
| `issues` | 検証境界で分類済みの項目別診断。今回対応する検証例外は`error_details.issues`に既存Enumの`field` / `code`を出力する。`expected_type` / `constraints`の自動抽出や、別のトップレベル`issues`の自動生成は行わない。 |
| `duration_ms` | 当該ログが表す業務試行またはAI呼び出しの実測経過時間。業務試行の終端ではメッセージ開始からの時間。 |
| `timeout_scope`, `timeout_phase`, `timeout_seconds`, `timeout_elapsed_ms` | scopeは`consumer` / `http` / `db`。phaseは観測できた`connect` / `read` / `write` / `pool` / `statement` / `lock`。有効期限と、その期限の対象範囲で測定できた経過時間。 |
| `ai_call_attempt`, `sqs_receive_count` | アプリが開始したAI呼び出しの通番と、SQSから取得した受信回数。どちらも正の整数。見えないSDK内部試行は数えない。 |
| `input_tokens`, `output_tokens`, `max_output_tokens` | providerが返した入力・出力使用量と、実際に指定した出力上限。非負整数。`completion_tokens`→`output_tokens`、DeepSeekの`max_tokens`→`max_output_tokens`とし、意味が一致する値だけを対応付ける。 |
| `failure_action` | 既存分類の業務副作用enum値。分類上の値であり実行完了を表さない。 |
| `message_disposition` | handlerが決定した`completed` / `batch_item_failure`。AWSへ返すバッチ応答への採用を表し、配信・再実行の完了を表さない。 |
| `persistence_state` | `committed` / `rolled_back` / `unknown`。対応する結果保存transactionで観測した状態のみ。commit中の切断は成功・失敗が確定しない限り`unknown`。 |
| `resource` | cleanup対象のコード所有名。既存の`rds` / `engine` / `http` / `sdk` / `sdk_sync` / `sdk_async`を使う。接続文字列全体を渡さない。 |
| `business_error_class` | 二次障害ログにおける元の業務例外型。二次例外は`exc_info`から`error_class` / `error_message` / `frames`へ変換し、`secondary_error_class` / `audit_error_class`を重複して渡さない。 |
| `slug`, `missing` | category enumとDBの不整合診断。既存enum由来のslugまたはそのリストのみ。AIが返した未検証のcategoryを入れない。 |
| `url` | 必要時に取得済みの公開記事URL。上位仕様§4.4の専用変換を通す。基底のuserinfo除去だけで保護完了にしない。 |

検証境界がPydanticの失敗を既存の業務例外へ分類し、アプリ用のログ変換は`reason`と`issues(field, code)`を`error_details.kind="application_validation"`へ写す。未知の入力キーは既存の`event` / `payload`と`unknown_field`へ集約した結果を使う。ログ側ではスキーマ・alias・Union・`loc`を再解釈せず、入力・任意ctx・元の説明文を添付しない。生のValidationErrorは基底で件数と標準分類だけを保持する。

イベント検証の対応は`AnalyzableEventInvalidError` / `IncompleteArticleEventInvalidError` / `CuratedEventInvalidError` / `AssessedEventInvalidError`の4種類とする。`build_processors`の既定の共通入口`convert_exception`からイベント検証専用の変換を呼び、原因とグループにも同じ入口を適用する。共通の`ApplicationError`は別の変換関数でメッセージと診断用辞書を受け渡す。実行環境へのロガー接続は別作業とする。追加の診断項目は、その境界で調査上の不足が確認された場合に検討する。

### 3.3 命名と記録単位

- 識別子は`log_policy=ai_inference`、モデルは`model`、時間は`*_ms` / `*_seconds`、使用量は`input_tokens` / `output_tokens`へ統一する。業務モデル・監査DBの属性名はログの命名に合わせて改名しない。
- `operation`の初期語彙は`settings` / `resources` / `ai_client` / `consumer` / `parse_message` / `validate_event` / `load_ready_facts` / `build_ready` / `build_prompt` / `ai_call` / `parse_response` / `validate_response` / `build_result` / `save_result` / `commit` / `audit` / `notification` / `processing_metric` / `audit_dropped_metric` / `failure_handling` / `cleanup`とする。`consumer`は既存のConsumer構築を表す。`resources`等の内部箇所はframeと原因で追う。細分化が必要なときは観測する処理境界とともに追加する。
- 既存の`stage=settings/resources/ai_client/consumer`は`operation`へ移す。正常終端の`reason`は`outcome`へ移す。二次障害の`audit_error_class` / `secondary_error_class`は`exc_info`由来の`error_class`へ揃える。
- Assessmentのメッセージ単位のeventは§3.3.1へ統一する。Assessment handlerは旧イベント名から切り替え済み。Curationへの適用も後続とし、AI呼び出しの既存`assessor_api_call/success`・`curator_api_call/success`は業務メッセージとは別の単位として扱う。eventにID・例外文を埋め込まない。

### 3.3.1 Assessmentの開始・完了・失敗ログ

この節の出力契約と目的別allowは、Assessment handlerの入口・終端へ接続済みである。イベントごとの専用クラスやポリシーは追加せず、一つの`AI_INFERENCE_LOG_RULES`を使う。

| イベント名 | 記録する条件・タイミング | level |
| --- | --- | --- |
| `assessment_message_processing_started` | 検証済みSQSメッセージIDを確認した後、本文の取得・解析前。 | INFO |
| `assessment_message_processing_completed` | 判定の保存完了または処理済みを確認し、そのメッセージの業務結果とSQS応答への扱いが確定した時点。 | INFO |
| `assessment_message_processing_failed` | 前提不成立または処理中の失敗について、業務結果とSQS応答への扱いが確定した時点。 | 前提不成立・契約上の入力不正はWARNING、処理例外はERROR |

処理開始後に結果を捕捉できたメッセージは、開始1回と完了または失敗1回を基本とする。初期化失敗・バッチ全体の入力不正・監査や通知の二次障害・cleanup失敗は、既存の別の記録境界に残す。キャンセルや強制終了について終端を捏造しない。

| 状況 | 終端イベントの末尾 | 結果・理由 | `message_disposition` |
| --- | --- | --- | --- |
| 対象内または対象外として保存完了 | `completed` | `outcome=in_scope` / `out_of_scope` | `completed` |
| DBで分析済みと確認 | `completed` | `outcome=already_assessed` | `completed` |
| Curation欠損 | `failed` | `rejection_code=assessment_ready_build_blocked_curation_missing` | `completed` |
| 分析入力の条件不成立 | `failed` | `rejection_code=assessment_ready_build_blocked_input_invalid` | `completed` |
| 既存契約でバッチ失敗応答に含める入力不正・処理例外 | `failed` | 既存の入力診断または例外診断 | `batch_item_failure` |

ログは業務処理の成否を表し、`message_disposition`はhandlerが決定したSQS応答への扱いを表す。前提不成立を失敗として記録しても、既存の受信完了・再配信・監査・メトリクスの動作は変えない。既存の拒否値をそのまま使い、ログのために例外化しない。

| 対象 | 出力項目 |
| --- | --- |
| 共通 | `event` / `timestamp` / `level` / `log_policy` / `service` / `environment` / `stage`。`stage=assessment`、`log_policy=ai_inference`。 |
| 開始 | `request_id` / `message_id`。`request_id`はLambda context、`message_id`は検証済みSQSレコード由来。 |
| 完了 | `request_id` / `message_id`、検証・取得済みの`event_id` / `curation_id` / `analyzable_article_id` / `analyzed_article_id`、`outcome` / `duration_ms` / `message_disposition`。 |
| 失敗 | 取得済みの相関ID・対象ID、確定できる`operation`、`duration_ms` / `message_disposition`。前提不成立なら`rejection_code`、例外なら既存の`exc_info`変換による診断項目。 |

未取得の項目は省略し、未検証の入力からIDを補完しない。`operation`は失敗した処理が確定している場合だけ記録する。`duration_ms`は`perf_counter()`で測ったメッセージ開始から終端直前までの経過時間をミリ秒で表す。

処理情報のallowは`service` / `environment` / `stage` / `operation` / `request_id` / `message_id` / `event_id` / `curation_id` / `analyzable_article_id` / `analyzed_article_id` / `outcome` / `rejection_code` / `duration_ms` / `message_disposition`。cleanup資源の識別には追加の`resource`を使う。内部ログでは`reason` / `business_error_class` / `code` / `finish_reason` / `max_output_tokens` / `error_class`もallowへ追加する。既存の`model` / `input_tokens` / `output_tokens`を維持する。基底5項目は継承し、processorが生成する`log_policy`と例外診断項目は目的別allowへ重複登録しない。ただし`error_class`はDeepSeek cleanupで明示するため登録する。

例外の分類・抽出・構造は既存処理に任せ、今回新設しない。`SqsInputError`と`AssessmentMessageJsonInvalidError`は`ApplicationError`として明示した診断を共通変換へ渡し、イベント検証例外も`exc_info`で渡す。本文10項目と認証情報のdeny・maskを維持し、§3.2の残りのallow、§3.4の追加保護、URL変換等の未実装要件をこの定義変更の完了に含めない。

### 3.3.2 Assessment内部の記録

handlerは検証済みイベント・対象IDをbindした`message_logger`をConsumerへ渡す。Consumerの`consume`、Serviceの`execute`、失敗後処理の`handle` / `handle_ready_build_rejected`は必須キーワード引数`logger: FilteringBoundLogger`で受け取り、同じロガーを引き継ぐ。メッセージ用ロガーをインスタンス属性に保存せず、内部で別のロガーを構築しない。

| 場面 | イベント・レベル | 記録内容 |
| --- | --- | --- |
| 保存commit成功 | `assessment_result_saved` / INFO | `outcome=in_scope`または`out_of_scope`。対象内のみ保存した`analyzed_article_id`を追加する。 |
| 同時処理による保存見送り | `assessment_result_save_skipped` / INFO | `reason=concurrent_write`。 |
| 失敗後処理全体の障害 | `assessment_consumer_failure_processing_failed` / WARNING | `operation=failure_handling`、`business_error_class`、二次例外の`exc_info`。 |
| 失敗監査の保存障害 | `assessment_consumer_failure_audit_dropped` / WARNING | `operation=audit`、`business_error_class`、二次例外の`exc_info`。 |
| 前提不成立の監査保存障害 | `assessment_ready_build_rejected_audit_dropped` / WARNING | `operation=audit`、`rejection_code`、二次例外の`exc_info`。元の業務例外型は付けない。 |
| 計測・通知の障害 | `assessment_consumer_failure_handling_failed` / WARNING | 既存の`processing_metric` / `audit_dropped_metric` / `notification`を`operation`とし、`business_error_class`と二次例外の`exc_info`を渡す。 |

相関情報はhandlerから引き継ぐ。内部ログにはメッセージ全体の`duration_ms`や`message_disposition`を追加しない。通常のログ障害は共通ラッパーで捕捉し、監査・通知・メトリクスの実行順序や元の例外伝播は維持する。前提不成立の監査drop計測失敗を抑止する既存処理も維持する。

Repositoryの起動時・保存時のカテゴリ整合性チェックは維持し、直接ログは削除する。`CategoryEnumDatabaseMismatchError`の既存メッセージが持つ不足カテゴリを、初期化・メッセージ失敗境界の`exc_info`経由で記録する。専用の`details`やRepositoryへのロガー引数は追加しない。

### 3.3.3 AssessmentのAI呼び出しとDeepSeek cleanup

Serviceから渡すメッセージ用ロガーを、DeepSeek・Gemini両方の`assess` / `_call_once` / `_call_api`が必須キーワード引数`logger: FilteringBoundLogger`で受け取る。`_call_once`でモデルをbindした派生ロガーを作り、開始・成功の記録と`_call_api`へ渡す。インスタンス属性には保持しない。compositionは`open_deepseek_client`へ呼び出し単位のロガーを渡し、cleanupにはメッセージ情報を持ち込まない。

| イベント | レベル | 記録内容・タイミング |
| --- | --- | --- |
| `assessor_api_call` | INFO | AI呼び出し前。相関情報・`model`。 |
| `assessor_api_success` | INFO | 応答解析・結果構築の成功後。相関情報・`model`。 |
| `assessment_deepseek_output_truncated` | WARNING | 打ち切り検出時。相関情報・`model`・`reason=output_token_limit_reached`・`output_tokens`・`max_output_tokens`。 |
| `assessment_deepseek_response_defect` | WARNING | 応答契約違反検出時。相関情報・`model`・既存の`code`・`finish_reason`・`output_tokens`・`max_output_tokens`。 |
| `deepseek_client_cleanup_failed` | WARNING | 資源終了失敗時。呼び出し情報・`operation=cleanup`・既存の`resource`（`http` / `sdk`）・例外型の完全修飾名を表す`error_class`。 |

`completion_tokens`を`output_tokens`、設定の`max_tokens`を`max_output_tokens`へ対応付ける。boolを除く非負整数のみを採用し、0は保持する。未取得・不正型は項目を省略する。`finish_reason`は`stop` / `length` / `tool_calls` / `content_filter` / `function_call`だけを許可し、未知値・不正型は省略する。`length`は従来どおり応答契約検証より先に打ち切りとして扱う。

この5イベントには`exc_info`・生の例外文・プロンプト・応答本文を渡さない。正常時の使用量ログや戻り値モデルの追加は行わない。既存の例外分類・原因連鎖・SDK設定・資源解放順序・キャンセル伝播は維持する。cleanupのログ障害は共通ラッパーに任せ、同じロガーによる再記録は行わない。

受け渡しはService・Assessor・compositionの既存テストで確認する。DeepSeekの項目変換とcleanupの出力は実際の目的ポリシー・processor・JSON標準出力で確認し、ログ全体の完全一致は使わない。マスク・共通例外変換・ログ障害保護・SQS応答はそれぞれの既存テストに任せる。

Assessment handlerの`setup_lambda_logging()`呼び出しは削除済みで、各目的別ロガーがJSON標準出力を構成する。共通関数本体と他工程の呼び出しは維持する。HTTP・AI SDK例外の専用変換は後続とし、handler等の既存終端ログが記録する原因連鎖には入力値が残る可能性がある。この接続を例外全体の保護完了とは扱わない。

検証結果: 関連単体テスト587件が成功（DB統合85件は選択対象外）。続くGeminiの公開`assess`経由へのテスト更新後も対象11件が成功した。変更したPython 16ファイルのRuff lint・format確認、DB利用ケースとローカルAssessmentテストを含む539件の収集確認が成功。全体・DB統合・実AI呼び出し・デプロイは実施していない。比較用スクリプトの呼び出しも必須logger引数へ対応させたが、実行はしていない。

### 3.4 禁止・マスク・サニタイズ

以下はINFO・WARNING・ERROR・DEBUG、構造化項目・ネスト・例外文のどこでも同じ保護対象とする。禁止部分を安全に分離できる原因文は、その前後の説明を残す。

| 対象 | 出さない情報・処理 |
| --- | --- |
| 認証情報 | 基底の`CREDENTIAL_KEYS`をdeny・maskとして継承する。パスワード、API key、Authorization、cookie、秘密鍵、セッショントークン等。既知の秘密形式はsanitizeでも保護する。独自に短い別リストへ置き換えない。 |
| 記事・生成本文 | 既存の`body`, `content`, `text`, `html`, `description`, `summary`, `translation`, `key_points`, `snippet`, `answer`をdeny・maskする。AI側には`original_content`, `original_title`, `title`, `title_ja`, `summary_ja`, `translated_title`, `investor_take`も追加する。タイトルも初期のAI診断には出さず、IDで対象を特定する。基底や他の目的のtitle規則は変更しない。 |
| prompt・応答 | AI側で`prompt`, `messages`, `request`, `response`, `payload`, `request_body`, `response_body`, `raw_response`, `raw_arguments`, `raw_category`, `raw_relevance`をdeny・maskする。部分抜粋も出さない。`prompt_version`・使用量・検証codeは別項目で残す。 |
| SQL実データ | AI側で`sql`, `statement`, `parameters`, `params`, `rows`をdeny・maskする。基底のSQL例外抽出でSQL本文・パラメーター・DETAIL/HINT/CONTEXT等を除去し、保護後のprimary messageと`error_details`内の診断属性を残す。今回SQLテンプレートの出力は許可しない。 |
| 検証input・任意dump | AI側で`input`, `ctx`, `config`, `settings`, `headers`, `locals`, `args`, `notes`, `__dict__`をdeny・maskする。SDKオブジェクト、設定全体、例外args/notes/__dict__、取得行を丸ごと出さない。診断用`issues`は§3.2の固定形状に再構成する。 |
| URL・内部宛先 | 公開記事URLの通常host/path/記事識別queryは保持する。userinfo・認証query・署名・内部IP/既知内部hostは上位仕様§4.4で保護する。例外文に混在する場合も対象とし、残る説明を一律に消さない。 |

追加キーはAI推論の目的ルールが所有し、他目的と共有する本文10項目の定義を無条件に拡大しない。キー照合は基底の正規化後の完全一致であり、`token`のdenyが`input_tokens`を禁止することはない。

denyは構造化項目をキーごと除外する。maskは文字列内のキー付き値を`***`へ置換し、sanitizeはAPI key形式・JWT・PEM・AWS認証情報・URL userinfo等の既知形式を基底どおりに置換する。これらを通すことを条件に通常の原因文を許可する。未知の自由文中の本文・秘密を完全検出できるという契約にはしない。

## 4. 失敗時の記録

### 4.1 必須となる診断

記録できる状態で失敗を捕捉した場合、event・stage・operation・例外型・保護後の原因文・発生箇所と、その時点で取得済みの相関IDを残す。既存のcode/reasonやprovider_errorが存在する場合は、外側の例外の`str()`だけで終わらせず抽出する。

原因文が空なら`[empty exception message]`、安全に分離できない禁止情報しかない場合は`[exception message omitted]`、抽出自体の失敗は基底の`[exception message unavailable]`で区別する。キー付き値だけを伏せた文には基底のマスク結果を使い、この省略表示へ一律置換しない。取得できないoperation・timeout段階等は項目を省略し、観測していない事実を補わない。空・省略の追加表示はAI記録側の未実装要件であり、現行基底の動作とは区別する。

Pythonのcause/contextは基底が抽出する。既存の`provider_error`のようにSDK固有の属性へ保持された原因も目的別実装の対象とするが、この取得は未実装である。外側と内側の例外を区別し、循環・深さ・件数を制限する。stackはlocals・生args・ソース行を含めず、アプリと依存ライブラリの発生箇所を追える形で保持する。

原因抽出は外側を含め最大32例外、外側から最大8段の関係までとし、ExceptionGroupのメンバーを含む全枝で予算を共有する。同じ参照の再登場も数え、現在の経路に戻る参照を循環として止める。直接causeを優先し、`provider_error`が同じ例外を指す場合は重複させない。contextは明示causeがなく、抑制されていない場合だけ辿る。原因は`causes`、グループのメンバーは`exceptions`へ格納する。上限・循環で省略した枝はそれぞれ`[limit]` / `[cycle]`で示し、メンバーの残りの省略は配列末尾の`[limit]`で示す。各frameの上限と最終ログの共有予算は基底を維持する。共有予算を超えれば基底の固定イベントになるため、原因連鎖を追加した出力で予算を検証し、保持できない代表ケースを未対応のまま完了扱いしない。

SDK例外のrequest/response bodyを含む表現は、既知の診断message・status・codeだけを抽出する。SQL例外の内側にあるdriver例外は、外側のSQL例外ノードの`error_details`へ診断属性を集約する。原因文は外側のパラメーター保護文脈で保護し、内部driverを別ノードへ再出力しない。SQLAlchemy例外だけを保護してから`orig`を通常の`str()`で再出力する迂回を認めない。未知の形式で混入部分を分離できなければ、その原因文を省略し、型・frame・分類・他の原因を残す。

SQLパラメーターや本文だけを除去できる場合は、その部分を除いて説明を残す。安全に分離できない部分を省略した場合も、他の原因説明・型・分類・frame・相関情報は保持する。未知の例外型という理由だけで全文を消さない。任意自由文の秘密・本文を完全検出できるという保証は置かず、既知の混入形式を共通変換とテストで扱う。

### 4.2 timeoutの位置

次の情報を別々に記録する。

1. 期限の範囲: 処理全体のdeadline、HTTP接続/read/write/pool、DBの待機等。
2. 期限切れ時に実行していたoperation。
3. 実際に適用されたtimeout値と、その範囲の経過時間。
4. 元の例外型・原因文・stack。

Consumer全体の期限切れを、providerのread timeoutとして表示しない。逆にHTTPのread timeoutが判明している場合は、単にAI処理失敗へ潰さない。外部からのキャンセルもtimeoutと混同せず、既存のキャンセル伝播を維持する。

現在の60秒をログ処理に複製せず、実際の適用値を実行側から渡す。timeout発生後に新しい通信やDBアクセスを行って診断情報を補完しない。

### 4.3 対処と二次障害

provider例外には回復分類・retryabilityを持たせない。DB障害などの`retryability=retryable`も再試行を実行した記録ではない。SQSバッチ失敗応答に含めた時点では「失敗項目として報告した」と記録し、将来の再配信完了まで断定しない。

保存済み、rollback済み、保存結果不明も区別する。commit中の通信断等で結果が不明なら、ログの都合で未保存と決めない。

元の分析失敗と監査・通知・cleanup等の二次障害は、それぞれの原因文・stack・operationを関連付けて残す。二次障害が元の例外や成功済みの業務結果を置き換えない。ログ自体の失敗は上位仕様の最小診断へ退避し、再帰的に同じloggerを呼ばない。

一次障害の詳細はメッセージ失敗ログに、二次障害の詳細はその処理境界の別ログに残し、取得済みのrequest/message/event/記事IDで結ぶ。二次障害ログは元の例外があれば`business_error_class`を伴い、二次例外の型は共通変換の`error_class`へ記録する。`operation`は失敗した監査・通知等を示す。cleanup等で元の業務例外が存在しない場合は捏造しない。記録障害は共通ラッパーで捕捉する。Assessmentの保存後ログとAI呼び出し内部・DeepSeek cleanupは共通ラッパーへ接続済みとする。

## 5. 正常時の記録と除外情報

| 場面 | 記録内容 |
| --- | --- |
| 処理開始 | stage、取得済み相関ID、対象ID、取得済みprovider/model。 |
| 正常終了 | 相関ID、処理時間、処理結果コード、取得できた使用量・保存先ID。 |
| 対象外・noise・処理済み | 既存の正常結果を残す。Assessmentでは§3.3.1の`outcome`を使う。 |
| Assessmentの前提不成立 | §3.3.1の失敗ログに`rejection_code`を記録し、再処理を要求しない判断を`message_disposition=completed`で示す。 |

処理開始と終端の記録は通常のINFO設定で確認できるようにする。すべての内部関数の成功ログやAI応答本文は要求しない。業務試行の入口・終端を所有する境界で記録し、各層から同じ完了ログを重複して出さない。内部のAI呼び出し試行を記録する場合は、業務試行と区別する。

| 所有する境界 | 記録責任・level |
| --- | --- |
| Lambda handler | 検証済みmessage IDを得た時点の開始と、各メッセージの正常完了をINFO。処理失敗はERROR、前提不成立・契約で扱う入力不正はWARNING。Assessmentは§3.3.1の3イベントを使う。バッチ全体の入力不正ではmessage IDを捏造しない。 |
| AI adapter | 呼び出し開始・正常終了をINFO。provider/model/prompt version、試行、実測時間、取得済み使用量。API応答の不備等の固有診断をWARNINGで出す場合も、業務試行の終端とは数えない。 |
| Consumer / Service / Repository | operationと分類・保存状態を確保して終端へ渡す。Handlerと同じ完了ログを重複させない。並行書き込み敗北・不整合など独立した事実の既存ログは役割を残す。 |
| 初期化・失敗後処理・cleanup | 実際に捕捉する境界で原因付きログを記録する。業務開始不能な初期化はERROR。元の結果を維持する二次障害・cleanup失敗はWARNING。 |

取得済みの使用量はAI呼び出しの終了地点で残せばよく、業務完了まで運ぶためだけに戻り値モデルや監査schemaを変更しない。各ログに相関情報を付け、保存失敗時にも直前のAI実行条件を追えるようにする。

開始だけが残る場合は終端を観測できていないことを示し、成功やtimeoutを推定しない。プロセス強制終了・OOM・Lambdaの強制終了など、アプリが捕捉できない障害はCloudWatchのランタイム・基盤情報と相関させる。アプリログだけで全障害の終端を保証しない。

以下は成功・失敗・debugのいずれでも通常ログへ出さない。

- 記事の生HTML・抽出本文・翻訳本文。
- 分析結果の説明文、investor take、key points等の生成内容、生成応答全体。
- prompt・会話本文・request/response body・検証inputのdumpや先頭抜粋。
- SQLパラメーター・取得行・秘密値・設定全体・例外locals等、上位仕様の禁止情報。

分析結果が必要なときは対象IDから既存の保存先を確認する。失敗して結果が保存されなかった場合も、応答本文のログ出力を自動で有効化しない。検証失敗はfield・code・期待する制約で調査する。

正常結果codeをログへ残すことと、分析内容を全文出力することは別の要件とする。既存メトリクスは維持し、詳細ログの項目をmetricラベルへ追加しない。

## 6. 診断例

以下は合成例であり、新しい業務分類や固定のtimeout値を導入するものではない。

| 入力・状況 | 出力に必要な内容 | 出力しないもの |
| --- | --- | --- |
| AIクライアントの構築で設定項目が不足 | Assessment/Curation、構築operation、設定項目名、原因文、file/function/line | 設定全体、API key |
| providerのread timeout、適用値30秒、２回目の呼出 | provider/model、read段階、30秒、実測時間、呼出試行２、元例外とwrapperの関係 | prompt、応答body、推測した再配信完了 |
| Consumer全体の期限が保存中に切れる | 処理全体のdeadline、保存operation、有効な期限、保存状態、stack | provider read timeoutという誤分類 |
| categoryの型が不正 | 応答検証operation、category、既存の型違反defect、期待する型 | 実際に生成された値、応答全体 |
| DB制約違反にSQLパラメーターが含まれる | SQLSTATE、制約名、保護後の原因文、対象ID、stack | パラメーター値、行データ |
| 分析失敗後、監査保存も失敗 | 元の分析原因と監査側の原因、それぞれのoperation・stack、相関ID | 元の失敗を消した監査エラーだけの記録 |
| 正常にout_of_scopeと判断 | 正常終端、out_of_scope、時間、対象ID、取得済み使用量 | 生成された判定理由の本文 |

## Implementation

2026-09-20は本書と上位仕様の文書だけを更新した。既存のログ出力・分類・設定・テストは変更していない。

2026-09-21の変更範囲は、§3.3.1の出力契約、目的別allow、既存の定義テストの期待値までだった。handler・共通ログ設定・例外変換・SQS応答・監査・メトリクスは変更せず、実行経路への接続は次項で実施した。

### Assessmentの入口・終端接続（2026-09-22）

- [AI記事分析ロガー](../../backend/app/analysis/logging.py)は、既存の目的ルール・factory・processor・JSON標準出力を明示して構築する。各呼び出しで生成し、グローバル設定から独立させる。AssessmentではLambda共通設定を呼ばず、グローバルstructlog設定も変更しない。ロガー引数の受け渡しとcontextvarsの束縛・復元は維持する。
- `service=article_analysis`、`stage=assessment`を付け、Lambda contextに有効な文字列があれば`request_id`、設定取得後は`settings.env`から`environment`を付ける。取得前の項目は省略する。呼び出し元のcontextvarsは退避・クリアし、`finally`で復元する。各メッセージの識別情報は派生ロガーに束縛する。
- handlerは§3.3.1の開始・終端を直接記録する。`operation`は本文取得・解析に`parse_message`、イベント契約違反に`validate_event`、前提不成立に`build_ready`を使う。Consumer内部の失敗箇所は推測せず省略する。
- 初期化は`assessment_initialization_failed`（ERROR）、バッチ不正は`assessment_sqs_input_invalid`（WARNING）、資源cleanupは`assessment_resources_cleanup_failed`（ERROR）を維持する。初期化箇所は`operation`、cleanupは`operation=cleanup`と`resource`に記録する。
- [共通ラッパー](../../backend/app/log_policy/bound_logger.py)の`ApplicationBoundLogger`を`wrapper_class`に指定し、`logger.info/warning/error`からprocessor・JSON化・出力までの`Exception`を捕捉する。生データによるfallbackや再帰的な再記録は行わず、`bind()`後も同じ保護を維持する。位置引数による文字列展開は保護範囲外とし、イベント名とキーワード項目で記録する。`BaseException`は抑止しない。ライフサイクル用の記録クラスには共有インターフェースの初期化・cleanupの2メソッドだけを残す。
- SQS応答・通知順序・監査・メトリクス・資源の所有権は維持する。Consumer・Service・失敗後処理の内部ログは§3.3.2へ接続済み。AssessmentのAI呼び出しと共有DeepSeekクライアント内部のcleanupは§3.3.3へ接続済み。通知処理内部とSSM cleanupは上位仕様§2.4の専用ルールを使用する。他工程・エージェントの業務ログ接続は後続とする。

以下の表は2026-09-21の定義時点における全体の差分整理であり、上記の部分接続以外は後続工程とする。

| 境界 | 実装済み | 接続・追加が必要な内容 |
| --- | --- | --- |
| 共通基底 | logger属性のルール、allow/deny/mask、外側の例外・SQL/検証の基本保護、上限 | 本書の追加診断を通した予算と最終出力の検証。基底の汎用allowを広げない。 |
| AI目的ルール | 本文10項目のdeny・mask、モデル・入力/出力tokensと§3.3.1・§3.3.2の処理情報のallow | §3.3.1・§3.3.2以外の追加allow・deny・mask、記録側の出所・値・ネスト形状の保証。 |
| 原因・検証診断 | 原因連鎖・ExceptionGroup・SQL診断・4種類のイベント検証例外の変換、分類reason・provider_error・応答defect等の内部保持 | 既存変換の実行経路への接続。SDK属性の追加診断、空・省略の区別等は後続工程であり、今回の定義変更では扱わない。 |
| 実行中の事実 | ID・正常結果・一部のモデル/使用量 | request/message文脈の設定と復元、operation、経過時間、timeout範囲、試行、実際の処遇・保存状態。 |
| 出力経路 | LambdaのJSON出力と各層のstructlog | factoryとprocessorの接続、各loggerのルール宣言、出力障害による業務結果の変更防止。共有AI clientのcleanupも対象。 |
| URL・内部宛先 | 基底の資格情報保護 | 公開記事URLと例外文の内部宛先の専用変換。未実装のまま保護済みとしない。 |

後続の接続・追加実装は次の境界で進める。今回の定義変更では以下を完了扱いにしない。

1. 既存の基底に本書の完成済みルールと診断抽出を追加し、必要情報の保持と禁止情報の非露出を合成入出力で検証する。
2. Assessmentの失敗記録へ接続し、初期化・処理全体・AI呼び出し・検証・保存・二次障害を確認する。渡されていないreason等の受け渡しも接続範囲に含める。
3. 同じポリシーをCurationへ接続し、工程固有の結果・検証情報が保持されることを確認する。
4. 通常時の開始・終端記録、context分離、CloudWatch到達を検証する。Logfire通常ログ転送の撤去は上位仕様の移行と整合させる。

`policy_logger` / `create_policy_logger` / `build_processors`を接続口として使う。共通のLambdaログ設定は他工程にも使われるため、切り替えで未移行工程の業務項目が消えない適用境界を実装前に定義する。AI分析の通常ログはCloudWatchへ切り替える。既存のLogfireの秘匿実装や例外の情報制限を前提にせず、必要な原因情報を発生元で補い、本書のポリシーで出力時に保護する。span/metricへの項目追加は別途判断する。本書のためにイベントごとの専用ロガーや別々のポリシー階層を導入しない。

## Verification

| ID | 独立した検証条件 | 必須の期待結果 |
| --- | --- | --- |
| A01 | 同じai_inferenceポリシーでAssessment/Curationを出力 | stageを区別し、共通の許可・除外規則が適用される。 |
| A02 | 設定・クライアント構築に失敗 | 対象記事IDが未取得でも、構築箇所・原因文・stackが残る。 |
| A03 | 同一provider例外型のtimeout/connection | 既存の詳細reasonと原因を区別できる。 |
| A04 | HTTP read timeout | 取得済みの段階・適用値・経過時間・試行回数が残る。 |
| A05 | DB保存中の処理全体deadline | 保存中と分かり、HTTP timeoutに誤分類しない。 |
| A06 | 通信段階・使用量・試行回数が取得不能 | 不明・未取得のまま扱い、0や1やreadを捏造しない。 |
| A07 | 入力構築・応答構築の検証失敗 | 両者を区別し、既知field・code・制約が残り、入力本文や未知キーは出ない。 |
| A08 | SQL例外の原因文・causeにパラメーター | SQL実データを除き、SQLSTATE・制約・説明・stackを保持する。 |
| A09 | TypeError等の想定外のコード例外 | 原因文とアプリ・ライブラリのframeが残り、型だけにならない。 |
| A10 | wrapperがprovider_errorやcauseを保持 | 元の原因・関係を残し、秘密値や本文を経由先から復活させない。 |
| A11 | 分析失敗後の監査・通知・cleanup失敗 | 各ケースで一次・二次障害を識別し、元の結果・例外伝播を維持する。 |
| A12 | provider失敗・DBなどの分類あり失敗 | providerの回復分類を補完しない。分類値の有無にかかわらず実際の対処を記録し、未来の再配信を断定しない。 |
| A13 | 正常な対象外・noise・処理済み | 正常な結果を識別でき、生成本文を出さない。 |
| A14 | 並行実行・バッチ内メッセージ・Lambda再利用 | stage・ID・operation・例外が別処理へ混ざらない。 |
| A15 | 共通変換またはログ出力が失敗 | 生データfallback・再帰なし。業務結果を変更しない。 |
| A16 | 通常INFO設定で開始と終端を出力 | Assessmentは§3.3.1のイベント名・level・項目に従い、開始1回と終端1回を相関できる。前提不成立は失敗ログでも受信完了を維持し、本文dumpや層ごとの重複がない。 |
| A17 | §3の既存名から出力名への対応 | model・tokens・outcome・stage/operationの意味が一致し、必要項目が未登録として落ちない。 |
| A18 | 各追加deny対象を構造化値・キー付き文字列・例外へ混入 | 各経路で値が残らず、同時に入れた通常の原因説明・ID・codeが残る。 |
| A19 | causes/issuesに未知キー・生input・任意ctx | 宣言した子項目だけが残る。トップレベルallowだけを試験して完了にしない。 |
| A20 | 原因連鎖・group・循環・上限ちょうどと超過 | 関係を追え、同一原因を重複展開せず、基底の共有予算と固定出力を維持する。 |
| A21 | 空の原因文・保護で省略した文・抽出失敗 | それぞれを区別し、他の診断情報を残す。 |
| A22 | Serviceの保存後またはAI呼び出し内の出力障害 | 成功を失敗へ変えず、元の業務例外も置き換えない。 |
| A23 | AI分析の設定接続と未移行Lambda/共有client | 未移行工程の既存項目を失わず、AI clientのcleanupも原因を記録できる。 |
| A24 | 公開URL・認証付きURL・例外文の内部宛先 | 通常の公開記事識別情報を保持し、資格情報・内部宛先を残さない。通信を追加しない。 |

既存の[Assessment Consumerテスト](../../backend/tests/analysis/assessment/test_consumer.py)、[分類テスト](../../backend/tests/analysis/assessment/test_consumer_failure_classification.py)、[失敗後処理テスト](../../backend/tests/analysis/assessment/test_consumer_failure_handler.py)、[応答解析テスト](../../backend/tests/analysis/assessment/ai/test_parse_assessment.py)、[Curation Consumerテスト](../../backend/tests/analysis/curation/test_consumer.py)を業務契約の根拠として維持する。

共通変換・目的ポリシーの出力は既存の所有テストで検証する。共通ラッパーによる出力障害の捕捉は`test_bound_logger.py`へ集約し、SQS応答・処理順序・資源解放は既存の業務テストで維持する。今回、ログ箇所ごとの文言・項目・出力回数の期待値を複製するテストは追加しない。CloudWatchの実配送は許可された検証環境で確認し、対象テスト成功を配送確認済みとは扱わない。

2026-09-20の文書検証では、ローカル参照先、JSON例の構文、許可名と禁止名の重複、受入条件の識別子、差分の空白を確認した。`/check`の文書変更規則に従い、実行コード・schema・依存・実行時設定を変更していないためコードのテストは未実行。実装時は`/check`を実行し、未検証経路と理由を記録する。

2026-09-21の定義変更は、app全体と変更テストのRuff lint・format確認、`uv run pytest tests/ -m unit -x -q`の7,244件、`make test-integration TEST_COMPOSE_PROJECT=vector-test-assessment-log-definition-20260921 PYTEST_ARGS='-x -q'`の1,291件が成功した。一時DB・Redisは終了・削除済み。新規テストは追加せず、既存定義テストの期待値を更新した。仕様と目的別allowの17項目の一致、既存deny・maskの維持、変更した2仕様のローカル参照43件、変更範囲が予定した4ファイルであることを確認した。handlerからstdoutまでの新契約の出力確認・AWS適用・CloudWatch到達確認は未実施であり、定義完了と区別する。

2026-09-22の接続変更では、共通記録口・チェーン・processor・AI目的ポリシー・例外変換と、Assessment handler・イベント・設定・共有ライフサイクル・SQS入力の対象テスト304件が成功した（DB統合4件は選択対象外）。今回変更したPython 9ファイルのRuff lint・format確認と差分の空白検査も成功した。ユーザー指定により検証は対象範囲に限定し、全体テスト・DB統合テスト・AWS適用は行っていない。

共通ラッパーへの置き換えと中断・終了・キャンセルのケース追加後、同じ対象範囲で307件成功・DB統合4件は選択対象外。PRで変更するPythonファイルのRuff lint・format確認も成功した。出力障害と中断の伝播のテストはラッパー側の5ケースに集約し、ログ箇所別のテストは追加していない。

2026-09-22の内部ログ接続では、Assessment・AI分析・handler・composition・共通ラッパー・AI目的ポリシー・例外変換の対象単体テスト600件が成功した（DB統合89件は選択対象外）。同じ対象の689件と`local_tests/assessment/`の19件の収集が成功した。変更したPython 13ファイルのRuff lint・format確認と差分の空白検査も成功した。DBテストは引数と観測用補助関数を更新したが実行していない。ユーザー指定により全体テスト・DB統合・ローカルDBテスト・AWS適用は行っていない。


## Done

Assessmentのログ定義（2026-09-21）:

- [x] 開始・完了・失敗の意味、イベント名、level、出力項目を§3.3.1で確定した。
- [x] 業務上の失敗とSQS応答への扱いを分離し、前提不成立を正常結果に含めない。
- [x] 目的別allowと既存の定義テストの期待値を更新し、基底・deny・mask・実行経路を維持した。
- [x] Assessmentの入口・終端と資源管理のログを共通チェーンへ接続した（2026-09-22）。
- [ ] AWS適用とCloudWatch到達は未確認。内部ログ・他工程は後続で移行する。

方針の整合・項目決定（2026-09-20）:

- [x] 調査に必要な原因情報を残す目的と、禁止・mask・sanitizeの責任を明記した。
- [x] 出力名・出所・値制約・記録境界を定義し、ベース仕様との違いと追加実装を区別した。
- [x] 必要情報の保持と禁止情報の非露出を両方確認する受入条件を定義した。

実装・接続（後続）:

- [ ] 一つのAI分析ポリシーでAssessmentとCurationのログを制御できる。
- [ ] 構築・通信・解析・検証・保存・想定外のコード不具合について、原因・箇所・条件を調べられる。
- [ ] timeoutの範囲、実行中operation、元の原因、実際の対処と二次障害を区別できる。
- [ ] 正常時の実行事実・結果codeを確認でき、分析結果本文をログへ出さない。
- [ ] 共通の秘匿・context分離・異常時契約を満たし、業務と既存metric/traceの動作を維持する。
- [ ] 実装リンク・検証結果・未対応経路を記載し、適用状況に合わせてStatusを更新する。
