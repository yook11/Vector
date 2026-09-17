# AI分析のログポリシー

作成: 2026-09-17
Status: Accepted
Implementation: 本文・派生テキストの禁止を `LogPolicy.AI_INFERENCE` の目的別 deny として定義済み。許可項目と既存 structlog への接続は未実装。

上位仕様: [アプリケーションログの概念別ポリシーとCloudWatch集約](./application-logging-policy.md)
関連: [#317 記録方針の共通化](https://github.com/yook11/Vector/issues/317)、[#328 エラーの情報保持と安全な記録の分離](https://github.com/yook11/Vector/issues/328)

## Problem

AI分析の失敗ログに例外型しか残らず、初期化・入力構築・通信・応答解析・保存のどこで何が起きたか調査できない。分析結果の全文を見ることよりも、失敗の原因、発生箇所、実行条件、対処を追えることを優先する。

正常時は実行の事実を簡潔に、失敗時は診断情報を詳しく記録する。AssessmentとCurationを同じAI分析ポリシーで扱い、工程は属性として区別する。

## Evidence

確認時のHEAD: `3706a4d1e`。以下はコード・テストの読解による確認であり、実環境の出力検証ではない。

| 対象 | 確認した事実 |
| --- | --- |
| Lambdaの失敗記録 | [Assessment](../../backend/app/lambda_handlers/assessment/failure_recorder.py)・[Curation](../../backend/app/lambda_handlers/curation/failure_recorder.py)の処理失敗ログはIDと例外型を記録し、原因文やstackを渡していない。 |
| 処理期限 | [Assessment Consumer](../../backend/app/analysis/assessment/consumer.py)・[Curation Consumer](../../backend/app/analysis/curation/consumer.py)は業務処理全体を60秒に制限し、失敗後処理はその期限の外で行う。 |
| 原因の分類 | [Assessment分類](../../backend/app/analysis/assessment/consumer_failure_classification.py)・[Curation分類](../../backend/app/analysis/curation/consumer_failure_classification.py)はproviderの詳細reasonを`failure_reason`へ投影する。 |
| 内部例外 | [Assessment errors](../../backend/app/analysis/assessment/errors.py)・[Curation errors](../../backend/app/analysis/curation/errors.py)は`provider_error`を保持するが、文字列はcode中心になる。 |
| 通信の原因 | [Gemini translator](../../backend/app/ai_providers/gemini/error_translator.py)はtimeoutとconnection等を区別する。ログ側で独自の分類を作り直す必要はない。 |
| 検証失敗 | [Assessment parse](../../backend/app/analysis/assessment/ai/parse.py)には欠落・型違反・値違反等のdefectがあり、未知の検証失敗を元の例外として伝える経路もある。 |
| 正常結果 | [Assessment Service](../../backend/app/analysis/assessment/service.py)には`in_scope`・`out_of_scope`・`already_assessed`、[Curation Service](../../backend/app/analysis/curation/service.py)にはsignal・noise・処理済みの区別がある。 |
| 対処と二次障害 | [Assessment handler](../../backend/app/lambda_handlers/assessment/handler.py)は失敗項目をバッチ応答へ含める。[失敗後処理](../../backend/app/analysis/assessment/consumer_failure_handling.py)は監査・計測・通知の障害を元の失敗と区別しているが、診断は主に例外型である。 |

## Invariants

- 共通規則の上に一つのAI分析ポリシーを置く。イベントやAssessment/CurationごとにAllow Listを複製しない。
- 原因説明、発生箇所、原因のつながり、既存の詳細reasonを保持し、すべてを例外型やunknownだけへ縮退させない。
- 診断可能なプロダクションコードの失敗は、AI provider由来かどうかにかかわらず対象にする。
- 記事本文・分析結果本文・prompt・応答全体・SQLパラメーター・認証情報は出さない。例外に混入した場合も同じ規則を適用する。
- 記録するのは取得できた事実と実際の判断であり、発生箇所・再試行・使用量等を推測しない。
- 記録処理は業務結果、例外伝播、再配信、transaction、監査保存の契約を変更しない。
- CloudWatch向けログの許可項目を、Logfireのspanやmetricへ無条件に追加しない。

## Non-goals

- 分析結果の内容や品質をログから閲覧・評価する機能、本文の別保存先の新設。
- Agentのユーザー質問、週次分析、Embedding等への一括適用。初期対象は記事のAssessmentとCurationとし、追加適用時に必要情報を確認する。
- 再試行戦略・timeout設定・例外分類・業務結果の変更、新しい計測サービスや依存の導入。
- 本書作成時点でのコード実装、structlog設定変更、AWS操作、Issue更新。

## 1. ポリシーの単位と責任

ポリシー名は既存の`app/analysis`の責務に合わせて`analysis`とする。AI分析処理の入口で選択し、`stage=assessment`または`stage=curation`で工程を区別する。初期化、実行、失敗後処理、cleanupまで同じポリシーを適用する。

`stage`は業務工程、`operation`はその中で実行していた処理を表す。現在初期化ログの`stage`へ渡している`settings`等は、接続時に`operation`として扱い、同じ名前に別の意味を持たせない。

| 担当 | 責任 |
| --- | --- |
| 処理入口・実行側 | ポリシーと相関情報を設定し、実行中のoperation・時間・利用設定を記録側へ渡す。 |
| 例外の生成・変換側 | 原因の意味と必要な事実を保持する。ログ表示の都合で原因を捨てない。 |
| 既存の分類・handler | 失敗分類と業務上の対処を決定する。 |
| 共通の記録処理 | 診断情報を抽出し、共通規則とanalysisの許可項目を適用してJSON化する。 |
| 実行基盤 | stdoutをCloudWatch Logsへ配送する。 |

Assessment中のDB例外も`analysis`の文脈で記録し、DB例外の抽出・秘匿規則は上位仕様の共通処理を使う。DBエラーという理由で相関情報を捨てたり、別ポリシーの許可項目を丸ごと合成したりしない。

本文・派生テキストの禁止はAI分析側の責任とし、認証情報は共通 deny を継承する。現在のコードでは `policies/ai_inference.py` が `policies/article_text.py` の禁止項目として `body` / `content` / `text` / `html` / `description` / `summary` / `translation` / `key_points` / `snippet` / `answer` を採用している。同じ本文禁止を外部コンテンツ取得にも適用する。共通の sanitize 処理は選択した目的の有効 deny を受け取り、ネスト値・既知キー付き文字列・例外文へ適用する。

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

以下を概念単位のAllow Listの契約とする。全項目を毎回埋めるという意味ではなく、各時点で取得済みの項目を選ぶ。未知項目・未知のネストは上位仕様どおりDropする。

| 項目・情報 | 規則 | 出所・制約 |
| --- | --- | --- |
| timestamp、level、service、environment、policy、event | 共通規則 | 基盤またはコードで定義した値。外部payloadによる上書き不可。 |
| stage | Allow | `assessment` / `curation`。 |
| operation | Allow | コードが識別する構築・読込・AI呼出・解析・検証・保存・commit・失敗後処理・cleanup等。任意の入力文字列ではない。 |
| request_id、message_id、event_id、trace_id、span_id | Allow | 実行基盤・検証済みイベント・tracerから得る相関ID。存在しないIDは生成済みとして扱わない。 |
| analyzable_article_id、curation_id、analyzed_article_id | Allow | 取得済みの正の整数。boolはIDとして扱わない。 |
| provider、model_name、prompt_version | Allow | 実際に使用する設定・adapter由来の識別子。promptの本文や設定dumpを渡さない。 |
| 処理結果・前提不成立のreason/code | Allow | 既存のCompletionKind・ReadyBuildの定義値を使う。本文の判定理由を入れない。 |
| code、failure_kind、failure_reason、retryability | Allow | 既存分類の値。通常の原因文と混ぜず、取得できた詳細reasonを落とさない。 |
| HTTP status、provider code、SQLSTATE、制約名 | Allow / Sanitize | 応答・例外の対応フィールドから取得。HTTP statusは有効な数値、各codeは定義形式。混在する禁止情報は共通規則で除く。 |
| error_class、error_message、frames、cause/context | Sanitize | 例外から抽出。通常の原因説明とアプリ・ライブラリ双方のfile/function/lineを保持する。 |
| 検証診断 | Sanitize | schemaに宣言されたfield、定義された違反code、期待する型・数値制約。未知field・任意ctx・生inputは出さない。 |
| 経過時間・有効なtimeout値 | Allow | 有限の非負数と明示した単位。どのoperation/timeoutに対応するかを伴う。未取得を0にしない。 |
| 試行回数・受信回数 | Allow | 正の整数。アプリの呼び出し試行とSQS受信回数を別の情報として扱う。未取得を1にしない。 |
| token使用量 | Allow | providerが返した既知の使用量項目と非負整数。未取得を0にせず、入力・出力の区別を保持する。 |
| 実際の対処・保存状態 | Allow | handlerやtransaction境界で確認した事実。分類から推定しない。 |
| 記事URL | Sanitize | 必要な場合だけ上位仕様の公開記事URL規則で処理する。取得のために追加通信しない。 |

`error_message`をAllowへ変更して共通のサニタイズを迂回しない。`frames`等の構造は子項目を明示的に抽出し、辞書全体や任意オブジェクトをシリアライズしない。

新たな実装フィールド名が必要な場合は、上表の意味に対応させ、既存名・単位・enumとの対応を実装時に記載する。項目を追加する判断はポリシーの正本で行い、個々のログ呼び出しに許可設定を置かない。

## 4. 失敗時の記録

### 4.1 必須となる診断

記録できる状態で失敗を捕捉した場合、event・stage・operation・例外型・保護後の原因文・発生箇所と、その時点で取得済みの相関IDを残す。既存のcode/reasonやprovider_errorが存在する場合は、外側の例外の`str()`だけで終わらせず抽出する。

原因文が空の場合は空であることを、禁止情報しか含まない場合は省略したことを固定の表示で区別する。取得できないoperation・timeout段階等は不明とし、観測していない事実を補わない。調査に必要な項目を取り出せない箇所は、接続時の不足として追跡する。

Pythonのcause/contextだけでなく、既存の`provider_error`のように明示的に保持された原因も対象とする。外側と内側の例外を区別し、循環・深さ・件数を制限する。stackはlocals・生args・ソース行を含めず、アプリと依存ライブラリの発生箇所を追える形で保持する。

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

`retryability=retryable`は再試行可能という分類であり、再試行を実行した記録ではない。SQSバッチ失敗応答に含めた時点では「失敗項目として報告した」と記録し、将来の再配信完了まで断定しない。

保存済み、rollback済み、保存結果不明も区別する。commit中の通信断等で結果が不明なら、ログの都合で未保存と決めない。

元の分析失敗と監査・通知・cleanup等の二次障害は、それぞれの原因文・stack・operationを関連付けて残す。二次障害が元の例外や成功済みの業務結果を置き換えない。ログ自体の失敗は上位仕様の最小診断へ退避し、再帰的に同じloggerを呼ばない。

## 5. 正常時の記録と除外情報

| 場面 | 記録内容 |
| --- | --- |
| 処理開始 | stage、取得済み相関ID、対象ID、取得済みprovider/model。 |
| 正常終了 | 相関ID、処理時間、処理結果コード、取得できた使用量・保存先ID。 |
| 対象外・noise・処理済み・前提不成立 | 既存の結果code/reasonを残し、例外による失敗と区別する。 |

処理開始と終端の記録は通常のINFO設定で確認できるようにする。すべての内部関数の成功ログやAI応答本文は要求しない。業務試行の入口・終端を所有する境界で記録し、各層から同じ完了ログを重複して出さない。内部のAI呼び出し試行を記録する場合は、業務試行と区別する。

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

文書のみ作成。既存のログ出力・分類・設定・テストは変更していない。

実装は次の境界で進める。

1. 共通規則と本ポリシーをデータとして定義し、許可項目・値制約・診断変換の合成入出力を検証する。
2. Assessmentの失敗記録へ接続し、初期化・処理全体・AI呼び出し・検証・保存・二次障害を確認する。渡されていないreason等の受け渡しも接続範囲に含める。
3. 同じポリシーをCurationへ接続し、工程固有の結果・検証情報が保持されることを確認する。
4. 通常時の開始・終端記録、context分離、CloudWatch到達を検証する。Logfire通常ログ転送の撤去は上位仕様の移行と整合させる。

共通processorの構造や具体的なAPIは後続実装で確定する。本書のためにイベントごとの専用ロガーや別々のポリシー階層を導入しない。

## Verification

| ID | 独立した検証条件 | 必須の期待結果 |
| --- | --- | --- |
| A01 | 同じanalysisポリシーでAssessment/Curationを出力 | stageを区別し、共通の許可・除外規則が適用される。 |
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
| A12 | 再試行可能な分類だが未実行 | retryabilityと実際の対処を区別し、未来の再配信を断定しない。 |
| A13 | 正常な対象外・noise・処理済み | 正常な結果を識別でき、生成本文を出さない。 |
| A14 | 並行実行・バッチ内メッセージ・Lambda再利用 | stage・ID・operation・例外が別処理へ混ざらない。 |
| A15 | 共通変換またはログ出力が失敗 | 生データfallback・再帰なし。業務結果を変更しない。 |
| A16 | 通常INFO設定で開始と終端を出力 | 実行を相関でき、成功ログの本文dumpや層ごとの重複がない。 |

既存の[Assessment Consumerテスト](../../backend/tests/analysis/assessment/test_consumer.py)、[分類テスト](../../backend/tests/analysis/assessment/test_consumer_failure_classification.py)、[失敗後処理テスト](../../backend/tests/analysis/assessment/test_consumer_failure_handler.py)、[応答解析テスト](../../backend/tests/analysis/assessment/ai/test_parse_assessment.py)、[Curation Consumerテスト](../../backend/tests/analysis/curation/test_consumer.py)を業務契約の根拠として維持する。

共通変換は単体で、呼び出し側からの原因伝達と最終stdoutは接続テストで検証する。CloudWatchの実配送は許可された検証環境で確認し、単体テスト成功を配送確認済みとは扱わない。

本書作成時は参照先・文書整合性の確認のみ。実行コードを変更していないためテストは未実行。実装時は`/check`を実行し、未検証経路と理由を記録する。

## Done

- [ ] 一つのAI分析ポリシーでAssessmentとCurationのログを制御できる。
- [ ] 構築・通信・解析・検証・保存・想定外のコード不具合について、原因・箇所・条件を調べられる。
- [ ] timeoutの範囲、実行中operation、元の原因、実際の対処と二次障害を区別できる。
- [ ] 正常時の実行事実・結果codeを確認でき、分析結果本文をログへ出さない。
- [ ] 共通の秘匿・context分離・異常時契約を満たし、業務と既存metric/traceの動作を維持する。
- [ ] 実装リンク・検証結果・未対応経路を記載し、適用状況に合わせてStatusを更新する。
