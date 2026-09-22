# アプリケーションログの共通基底ポリシー

作成: 2026-09-17
Status: Partially implemented
Implementation: 基底規則・processor・例外構造化・チェーン構成を `backend/app/log_policy/` に実装済み。本文の目的別 deny・mask を定義済み。AI推論のモデル・トークン数allowとロガー属性での完成済みルール保持を実装済み。その他の目的別allowと既存structlogチェーンの置き換えは未実施。

関連: [アプリケーションログの概念別ポリシーとCloudWatch集約](./application-logging-policy.md)、[デプロイ診断ログの共通秘匿ポリシー](../platform/deployment-log-policy.md)

## Problem

structlog の処理チェーンに共通の禁止規則がなく、秘匿は呼び出し側の規律 (`redact_secrets` の手巻き、`logger.exception()` の回避) に依存している。結果として、例外文を丸ごと捨てる箇所と生で出す箇所が混在する。

ログポリシーは「処理の目的」ごとに定義し、同じ目的の処理は bounded context が違っても同じポリシーを使う。本書はその土台として、**目的に依存しない基本ログ項目のallowと認証情報のdeny・mask**を基底として定める。迷うものは基底に入れず、実装中に必要性が見えた時点で追加する。

## Evidence

| 対象 | 現状 |
| --- | --- |
| [setup.py](../../backend/app/logfire/setup.py) | production では `format_exc_info` が生 traceback (`str(exc)` 込み) を JSON に焼く。チェーンに秘匿 processor はない。 |
| [lambda_handlers/logging.py](../../backend/app/lambda_handlers/logging.py) | 別の structlog 設定。保護 processor なし。 |
| [redaction.py](../../backend/app/shared/security/redaction.py) | 既知 secret 8 パターン。呼び出し側が巻いた `error_message` にしか効かない。 |
| 生 `str(exc)` の出力 | [acquisition.py:203](../../backend/app/queue/tasks/acquisition.py)、[backfill.py:180](../../backend/app/queue/tasks/backfill.py)、[rss_fetcher.py:73](../../backend/app/collection/article_acquisition/rss_fetcher.py)、[scraper.py:153](../../backend/app/collection/article_completion/scraper.py) |
| [engine.py:100](../../backend/app/db/engine.py) | `hide_parameters=True`。発生元の防御であり、driver 由来 message には効かない。 |
| [deployment-log-policy.md §2](../platform/deployment-log-policy.md) | S1 (パスワード・秘密鍵) / S2 (認証・セッショントークン) の分類。本書の認証情報はこれと同一定義を使う。 |

## 構成

```text
共通基底 (本書)          基本項目のallowと認証情報のdeny・mask
  └─ 目的ポリシー (別途)  基底allow・deny・maskを含め、目的別allowを明示する
       外部コンテンツ取得 / AI 推論 / 利用者対話 / パイプライン制御 / 基盤接続
```

`deny`・`allow` は構造化項目の選別、`mask` は文字列内のキー付き値の置換を指定する。認証情報のdeny・maskと基本項目のallowは基底、本文などの業務データのdeny・maskと追加allowは目的別ポリシーが所有する。

| 指定 | 動作 | 記録 |
| --- | --- | --- |
| `deny` | キーごと落とす。allow に入っていても効く | `_denied_keys` にキー名 |
| `allow` | 値を出す。文字列は呼び出し側で sanitize → mask を適用する | — |
| `mask` | 文字列内の指定キーに対応する値を内容によらず `***` に置換する。辞書の値全体は置換しない | — |
| 未登録 | トップレベルでdeny・allowに該当しない。maskの有無に関係なくキーごと落とす | `_unregistered_count` に件数 |

- 継承はクラス継承ではなく、`LogPolicyRules.extend(*, allow, deny=frozenset(), mask=frozenset())` で新しい規則を作る操作で表す。直接生成時は基底allow・deny・maskを必ず含め、継承時は親のdeny・maskへそれぞれ追加分を足す。親の識別子を維持し、親の規則は変更しない。
- `allow` は継承時に必ず明示し、親の目的別allowは自動追加しない。基底allowは常に自動追加する。`allow ∩ deny = ∅` を正規化後に構築時点で検証し、共通・親・子の禁止をallowで解除する定義を拒否する。
- `mask` は `allow`・`deny` と重複できるが、項目の採用・除外には影響しない。追加denyを文字列でも保護する場合は同じキーをmaskへ明示する。
- `LogPolicyRules.allow`・`deny`・`mask` は基底・継承分を含む正規化済みの確定集合として構築時に保持する。processorは登録表を持たず、`PolicyLogger.rules` に保持された完成済みルールを直接適用する。同じ識別子のlogger同士でも規則を上書きし合わない。
- processorは確定したdenyをフィールド選別・ネスト内の除外へ、確定したmaskを通常値・例外・診断の文字列準備へ渡し、下流で再合成しない。`create_policy_logger` はルール省略時に `BASE_LOG_RULES` を使い、指定値が厳密に `LogPolicyRules` 型でなければ生成時に拒否する。共通設定下の通常の `structlog.get_logger()` もこのfactoryを通る。processorはログごとの型確認や基本ルールへの差し戻しを行わず、factory未接続などの処理失敗では既存の固定エラーを返す。
- deny・allow・mask はいずれもキー名の正規化後 (小文字・camelCase / PascalCase / 略語境界 / ハイフン → snake_case) の**完全一致**で判定する。部分一致や正規表現を使うと `completion_tokens` `rds_iam_auth_token_port` 等を巻き込み、例外リストが必要になる。基底に例外 (逃げ道) を置かないための条件である。
- `policy_logger(name, rules=BASE_LOG_RULES)` は名前とルールを `wrap_logger` の `logger_factory_args` に渡す。structlog既存の遅延proxyが初回利用時に共通設定の `create_policy_logger` を呼び、ルールを属性に持つ出力先ロガーを生成する。ルールは `event_dict` に入らず、ログ引数・bind・contextvarsから変更できない。`PolicyLogger` はルールの再代入を禁止し、出力処理を内包した出力先へ委譲する。基底ルールの `policy` は `None` とし、目的別の場合だけ `log_policy` に識別子を出力する。
- 明示禁止は IAM と同じ `deny` と呼ぶ (explicit deny は allow に常に優先する)。OpenTelemetry Collector の redaction processor は allow-list 非該当の削除を "redacted"、値の置換を "masked" と呼ぶため `redact` は使わない。Pydantic の `extra="forbid"` (未登録拒否に近い別概念) と混同しないよう `forbid` も使わない。
- アプリログは Logfire から切り離す。Logfire は trace / span 専用とし、structlog のチェーンは本ポリシーが独自に構成して stdout JSON → CloudWatch へ出す。
- 階層 (public / restricted) は出力先の軸であり本書では扱わない。アプリの stdout → CloudWatch は restricted 単一とする。

## Invariants

- 共通の認証情報禁止・例外保護・未登録除外は、どの目的の処理からも、bind / contextvars / ログ引数のどこから来た値にも同じく適用される。
- 基底のキー deny は認証情報だけを定める。基本5項目のallowは `BASE_ALLOW` に定義し、本文の禁止と追加allowは目的別に持つ。
- 目的ポリシーは基底の deny・mask を独立して継承し、それぞれ足すことしかできない。基底より緩いポリシーは定義できない。
- 適用は structlog processor で行い、呼び出し側の手巻きに依存しない。renderer や Logfire 転送より前に確定させる。
- 秘匿処理の失敗で業務結果・例外伝播・再試行を変えない。原文 fallback はしない。

## 基底 v1

### 1. 認証情報

「知っていれば他人になりすませる値」。キーと値の両方で扱う。

| 種別 | 対象 |
| --- | --- |
| AWS | access key ID、secret access key、session token、SigV4 署名、RDS IAM auth token、ECR login password |
| DB / Redis | パスワード、DSN の userinfo |
| 外部 API | Gemini / DeepSeek / Tavily / Logfire の API key、GitHub token |
| HTTP | `Authorization` / `Proxy-Authorization`、JWT、認証 cookie |

- deny キーの正本は `base.py` の `CREDENTIAL_KEYS`。`password` / `passwd` / `pgpassword`、`secret` / `private_key` / `client_secret`、`token` / `access_token` / `refresh_token` / `id_token`、`api_key` / `x_api_key` / `x_goog_api_key`、`authorization` / `proxy_authorization`、`cookie` / `set_cookie`、`access_key_id` / `secret_access_key` / `session_token` と各 `aws_` 接頭辞版、`x_amz_signature` / `x_amz_credential` / `x_amz_security_token` を含む。正規化後の完全一致で、値の長さや見た目によらず落とす。
- アプリ固有の設定名も明示 deny に含める: `gemini_api_key` / `openai_api_key` / `deepseek_api_key` / `tavily_api_key` / `logfire_token` / `bff_jwt_signing_secret` / `revalidate_bearer_secret` / `postgres_auth_password` / `postgres_app_password` / `postgres_collect_password`。`app/config.py` と `app/db/settings.py` の認証情報定義を確認済み。
- 値の準備: sanitizeは内容からprovider key / JWT / PEM秘密鍵 / AWS認証形式 / DSN userinfoを検出して置換し、maskは指定キーの値を内容によらず置換する。キー付き値は長さを問わず、引用符内の空白・改行・エスケープを値全体として扱う。閉じていない引用符は末尾まで伏せる。引用符なしの一般値は空白等までの単一トークン、認証ヘッダー・cookie は行末までを対象とする。任意の文字列や別名で渡された認証情報の完全検出は保証しない。パターン集は deployment-log-policy の S1/S2 表に対応し、`log_policy` がログ出力用の正本として所有する。監査 DB `error_message` 用の既存 `redact_secrets` (`app/shared/security/`) はこの step では変更せず、監査経路を本ポリシーへ寄せるかは組み込み step で判断する。
- **含めないもの**: ARN、account ID、endpoint、host、SSM parameter path。これらは識別情報であり、基盤接続ポリシーが allow を判断する。DSN は「userinfo を伏せ host を残す」変換になるが、基底が保証するのは userinfo 部分だけである。

#### AWS・JWTの対象別サニタイズ規則

`sanitize.py` の対象別関数は文字列内の認証値を置換する。フィールドの除外は担当せず、deny優先の判定は既存の選別処理が担う。

| 処理 | 検出対象 | 置換結果・保持する情報 |
| --- | --- | --- |
| `sanitize_aws_access_key_ids` | `AKIA` / `ASIA` に英大文字・数字16文字が続き、前後が英大文字・数字ではないID | ID全体を既存の `AKIA***` 表記に置換し、周囲の文を残す。 |
| `sanitize_aws_signed_query_credentials` | `X-Amz-Signature` / `X-Amz-Credential` / `X-Amz-Security-Token` の `=` 以降の非空値（名前は大文字小文字不問） | `&`・空白・引用符までの値を `***` に置換し、クエリ名、接続先、その他のパラメーターを残す。RDS IAM認証文字列にも適用する。 |
| `sanitize_jwts` | 先頭2区画が `eyJ` で始まり、それぞれに後続文字がある3区画のbase64url文字列 | 3区画を `eyJ***` に置換し、周囲の文を残す。デコード・署名検証はせず、すべてのJWT表現の検出は保証しない。 |

AWS secret access key / session tokenなど、単独の値から識別できない認証情報は、共通maskによるキー付き値の処理が担当する。`Authorization` に含まれるJWTは内容検出後に認証方式も含めてヘッダー値全体をマスクする。

`sanitize_text(text)` は、PEM秘密鍵 → provider key → AWSアクセスキーID → AWS署名付きクエリ → URL userinfo → JWTの順に内容の検出処理を呼ぶ。ポリシーのキー集合は受け取らない。値準備と診断の出力箇所で `sanitize_text` → `mask_assignments` を直接呼び、各文字列を一度ずつ出力用に整える。両者を呼ぶだけのヘルパーは設けない。URL userinfoは `sanitize_url_userinfo` がschemeと接続先を残して置換する。

PEM秘密鍵はキー付き値の処理より先に保護する。`private_key=-----BEGIN PRIVATE KEY-----` を単一トークンとして先に置換するとBEGIN境界が失われ、後続の秘密鍵本文を検出できなくなるためである。

文字列処理へ渡す `mask` は、基底・継承分を含む正規化済みの確定集合とする。`mask_assignments` へ明示的に渡し、処理自身は基底maskの追加や目的別定義の解決を行わない。単独の `LogValuePreparer` は既定値に `BASE_MASK` を使う。

認証ヘッダー・cookieは既存の専用規則を優先し、引用符付き値は閉じ引用符まで、それ以外は括弧で始まっていても行末または `}` の手前まで伏せる。その他のキー付き値の終端は `:` / `=` の後の空白を除いた先頭文字で判定する。引用符ならエスケープを考慮した閉じ引用符まで、`[` / `{` / `(` なら引用符内の括弧を無視し、入れ子を追跡した対応する閉じ括弧までを一つの値として `***` にする。JSON文字列・Pythonの辞書や配列表現・例外文でも同じ規則を適用し、保護対象の内部の項目名や値の型には依存しない。引用符・括弧が閉じない場合や対応しない閉じ括弧を検出した場合は値の開始から文字列末尾まで伏せ、後続項目らしい文字列から処理を再開しない。これは値の範囲の判定であり、JSONやPython構文全体の妥当性検証や値への復元は行わない。それ以外の先頭文字では既存の引用符なし値の区切り規則を維持する。

対象別の検証は `test_sanitize_aws.py` / `test_sanitize_jwt.py` / `test_sanitize_url.py` が担当し、`test_sanitize.py` で内容検出、`test_mask.py` でキー付き値のマスク、`test_text_preparation.py` で両者の合成を検証する。

`test_mask.py` では値全体の除去と安全な前後の診断保持を期待文字列で確認し、正常な終端・入れ子・引用符・壊れた終端を別条件として扱う。`test_mask_rendering.py` では本文を保護する目的ポリシーの代表としてAI推論の規則を共通チェーンに通し、JSON文字列・Python表現・例外文へ表現が変わってもJSON / consoleの最終出力に保護対象の各要素が残らないことを確認する。認証情報と本文の検証は分ける。両目的ポリシーが本文を守ることは `test_policy_boundaries.py`、ネストの禁止項目が renderer 後も残らないことは `test_chain.py` が担当する。

URLは `://` を起点に左側のschemeを確認し、隣接する数字・記号の後ろのURLも保護する。JWTはbase64url区画を一度ずつ走査し、不成立の接頭辞ごとに長い接尾部を再走査しない。検出結果の比較と実測は[性能測定記録](./log-sanitization-benchmark-2026-09-18.md)を参照。その後の入力上限と項目単位の置換は、以下の「入力上限と置換単位」に定める。

### 2. 目的別 deny・mask との境界（記事本文）

`BASE_DENY = CREDENTIAL_KEYS`・`BASE_MASK = CREDENTIAL_KEYS` とし、本文・派生テキストは含めない。`policies/article_text.py` の禁止項目を `policies/external_content.py` と `policies/ai_inference.py` がそれぞれdeny・maskへ明示的に採用し、共通deny・maskを継承した規則として定義する。目的別モジュール内の `extend(allow=..., deny=..., mask=...)` で利用する規則を完成させ、loggerへ渡す。識別子から規則を検索したり、denyからmaskを暗黙に作ったりしない。

```python
from functools import partial

logger = policy_logger("article_analysis", AI_INFERENCE_LOG_RULES)
base_logger = policy_logger("infrastructure", BASE_LOG_RULES)
structlog.configure(
    logger_factory=partial(
        create_policy_logger,
        output_logger_factory=structlog.PrintLoggerFactory(),
    ),
    processors=build_processors(renderer),
    cache_logger_on_first_use=True,
)
```

`partial` は出力先の生成方法だけを関数の引数へ設定するために使う。rendererはプロセス共通の設定で決め、個別の `policy_logger` はprocessorsやrendererを固定しない。独自の遅延生成やルール登録表は設けない。`cache_logger_on_first_use=True` は初回に生成した本体を再利用し、テストなどで再設定が必要な場合は無効にする。

`AI_INFERENCE_LOG_RULES` は基底に加えて `model` / `input_tokens` / `output_tokens` を許可する。その他の業務項目と外部コンテンツ取得の追加allowは別途定義する。

基盤接続などでは `description` を明示 allow すれば診断情報として残せる。共通 deny から外した項目も、allow に未登録なら引き続き出力しない。本文を扱う目的を増やす場合は、その目的の deny を明示する。

- deny キー: `body` `content` `text` `html` `description` `summary` `translation` `key_points` `snippet` `answer`
- 上記2目的での対象は取得 HTML、抽出本文、整形本文、翻訳、要点、引用抜粋、AI 生成文。先頭 N 文字や debug レベルを例外にしない。
- 代替は `body_length` `has_body` 等の計量値と記事 ID・URL。
- `title` は v1 では対象にしない。
- dict / list / tuple 内でも禁止キーを再帰的に除外する。選択した目的の確定済みmaskを文字列・例外文のマスク処理へ渡し、`content='...'` 等のキー付きの表現を伏せる。本文用maskを指定していない目的へ暗黙適用しない。自由文として埋め込まれた記事本文の完全検出は保証しない。
- 入力値を漏らさない例外保護は引き続き共通とする。生のPydantic `ValidationError` は `str(exc)` を使わず、件数と標準エラー分類だけを抽出する。`input` / `ctx` のほか、入力を含み得る `msg` / `loc` / `title` / カスタム分類文字列も出さない。アプリの検証境界が分類済みの理由・項目・コードは、後述のアプリ用変換で取り出す。

### 3. SQL診断とパラメータ

SQLAlchemy例外の原因文からSQL実データを除き、診断属性は`error_details`へ独立して保持する。共通の`error_class` / `error_message` / `frames`は維持し、トップレベルの`sqlstate`は出さない。

- `error_details`の共通型は`types.py`の`ErrorDetails = Mapping[str, object]`とし、各変換が生成した文字列キーの診断用辞書を受け渡す。固有フィールドの型は各担当側で定義し、共通型では種類を列挙しない。PostgreSQLの診断は`PostgresErrorDetails`で、`kind: "postgresql"`と任意の`sqlstate` / `schema_name` / `table_name` / `column_name` / `constraint_name` / `data_type_name`だけを持つ。有効なSQL診断値が一つもない場合は項目自体を省略する。
- SQLAlchemyの`orig`から最大8例外を、循環検出とcontextの表示抑制を守って探索する。asyncpgの`PostgresError`を取得元とし、元例外を取得できない場合だけ既知のSQLAlchemy asyncpg adapterの診断を使う。同名属性があるだけの未知例外をPostgreSQLと判定せず、異なる例外から診断属性を寄せ集めない。
- `sqlstate` / `pgcode`は順に検査し、英大文字・数字5文字の最初の有効値を`error_details.sqlstate`へ入れる。対象名は空でない組み込み文字列のみを採用する。欠落・不正値・取得に失敗した属性は省略し、他の正常な属性を残す。
- `hide_parameters=True`を維持する。原因文は`str(exc)`を使わず`args[0]`から取得し、`DETAIL` / `HINT` / `CONTEXT` / `QUERY` / `STATEMENT` / `LINE N`以降を除く。原因文に含まれるbind値と既知のエスケープ表現を置換する。
- パラメータとの部分一致置換は原因文だけに適用し、診断属性へ適用しない。診断属性にも共通sanitize・目的別mask・文字数上限を適用する。SQL本文・パラメータ・行データ・補足文や任意属性は転記しない。
- パラメータが循環・深さ超過・件数超過・独自型の場合は原因文を固定文にするが、診断属性は維持する。原因文抽出の失敗も`[exception message unavailable]`に局所化する。
- パラメータの保護文脈がないasyncpg例外・adapter例外を直接受け取った場合は、診断属性を取得し、原因文を`[exception message omitted]`にする。未知のdriver独自表現に含まれる値の完全検出は保証しない。

### 共通アプリケーション例外の診断

`app/shared/errors.py`の`ApplicationError`はログに依存せず、メッセージと任意の診断用辞書`details`を保持する。値の型注釈はJSONで表せる値を対象とする。`convert_application_error`は`str(exc)`と`details`を`ConvertedException`へ写し、診断情報が`None`なら最終出力の`error_details`を省略する。取得に失敗した場合は`[exception message unavailable]`を返し、任意属性へのfallbackは行わない。後段の共通sanitize・mask・出力制限を適用する。`SqsInputError`と`AssessmentMessageJsonInvalidError`へ適用し、Assessmentの入口・終端のログ経路へ接続済み。他のLambda・内部ログへの適用は後続とする。

### アプリの検証診断

検証境界はPydanticの失敗を既存の業務例外へ分類し、ログ側はその例外が持つ`invalid.reason`と`invalid.issues`を出力形式へ写す。スキーマ・alias・Union・`loc`の汎用解釈、全フィールドの許可リスト、数値制約の自動抽出は行わない。

- `event_validation.py`の`convert_event_validation_exception`の対象は`AnalyzableEventInvalidError` / `IncompleteArticleEventInvalidError` / `CuratedEventInvalidError` / `AssessedEventInvalidError`の4種類。同名属性があるだけの未知例外は対象にしない。共通入口`convert_exception`がイベント検証・SQL・生のValidationError・ApplicationError・通常例外の順に各変換へ振り分ける。
- `event_validation.py`の`EventValidationDetails`は`kind: "application_validation"`、既存Enumの値による`reason`、`issues: [{field, code}]`を持つ。項目の順序・重複は変更せず、任意属性・元の入力を転記しない。
- 原因文は`Validation failed: {reason}`。属性取得・変換に失敗した場合は`[exception message unavailable]`とし、診断を省略する。元の例外の`str()`へ戻さない。
- `cause_is_aggregated`は偽で、通常の原因探索を維持する。境界での分類・Enum・再送出方法は変更しない。
- `build_processors` → `LogPolicyProcessor` → `extract_exception_fields`のキーワード専用引数`exception_converter`で変換担当を渡す。既定値は全種類を振り分ける共通入口`convert_exception`とし、アプリ用の別入口は設けない。引数による変換担当の差し替え口は維持する。種類固有の情報抽出は各変換に閉じ、共通結果型は`types.py`で定義する。
- 実行環境のロガー設定・Logfireへの接続は対象外で、既定チェーンでの出力までを保証する。

```json
{"error_message":"Validation failed: invalid_payload","error_details":{"kind":"application_validation","reason":"invalid_payload","issues":[{"field":"payload.curation_id","code":"missing_required_field"}]}}
```

### 原因チェーン

DB層はアプリ用例外への変換と`raise ... from exc`による原因の保持を担う。ログ側はその業務分類を変更せず、通常の例外連鎖を辿る。

- `causes`の各要素は外側と同じ`ExceptionLogFields`とし、各例外自身の型・原因文・frame・任意の`error_details`・子の`causes`・グループの`exceptions`を持つ。取得元を示す関係ラベルは付けず、内側の診断属性を親へ引き上げない。
- `__cause__`を優先し、明示causeがなく`__suppress_context__`が偽の場合だけ`__context__`を辿る。`raise ... from None`を尊重する。
- 最大32例外・外側から最大8段の関係までとし、原因・グループのメンバーで総数と深さを共有する。外側を1件とし、子の型判定・属性取得の前に上限を確認する。同じ参照の再登場や循環確認も総数に含め、現在の経路に戻る参照だけを`[cycle]`とする。
- `BaseExceptionGroup`のメンバーは`exceptions`に格納する。原因を先に辿り、メンバーは元の順序で各原因まで展開する。上限までの情報を残し、省略する原因は`causes: "[limit]"`、メンバーの残りは`exceptions`の末尾の`"[limit]"`で示し、その先の走査を止める。
- SQL内部のadapter・driver例外はSQL例外ノードへ集約し、それより内側を別ノードで文字列化しない。これにより、`orig`の原文から実データが再出力される迂回を防ぐ。
- `error_details` / `causes` / `exceptions`は共通ロガーが例外から生成する出力専用項目とし、ログ引数・bind・contextvars由来の同名項目は正規化後に除外する。allow宣言があっても入力辞書を受け入れない。
- 各原因・メンバーノードにも同じ値準備と共有予算を適用する。通常のcause/contextとExceptionGroupの子展開を基底が所有し、`provider_error`などSDK固有の取得は別途対応する。

共通探索は上限・循環を確認してから指定された `exception_converter` を呼び、例外1件の原因文・診断属性・内部原因の集約状態を受け取る。同じ変換担当を原因とグループの各ノードへ引き継ぎ、集約済みの内部原因は再展開しない。総数上限は `EXCEPTION_LIMIT`、深さ上限は `CAUSE_DEPTH_LIMIT` で定義する。

### 4. 未登録キー

宣言されたポリシーのallowにもdenyにも該当しないトップレベルのキーは、maskの指定に関係なくキーごと落とす。

- 未登録キーは名前・値とも出さず、`_unregistered_count` に件数だけを記録する。top-level でも kwargs 展開によって入力由来の名前が入り得るため、コード由来と仮定しない。
- deny に該当した場合は `_denied_keys` に記録し、未登録と区別する。前者は bug、後者は宣言漏れ。
- 目的ポリシー導入後、正常系で `_unregistered_count` が出ないことを契約テストで検証する（全体へのCI適用は未実施）。

### 保護処理の境界と失敗時

- `event` / `level` / `timestamp` / `logger` / `logger_name` は `BASE_ALLOW` に含め、allow選別で特別扱いしない。基本項目も通常の値と同じ構造検査・deny除外・サニタイズを通し、キー名を理由とする文字列型の強制は行わない。`level` と `timestamp` はチェーン前段で生成する。ConsoleRendererが要求するlevelの文字列型はこのチェーンで保証し、processor単体から直接接続する場合にはこの保証はない。
- `stack` / `stack_info` / `exception` / `_record` / `_from_structlog` / `_log_policy_rules` / `exc_info` は renderer へ転送しない。スタック調査には `exc_info` から抽出した frame metadata を使う。
- 出力値は組み込みの JSON 相当型に限定する。bytes / set / 独自オブジェクト / 組み込み型の subclass は固定マーカーにし、`repr` / `__structlog__` 等を呼ばない。
- 単一文字列4000文字、イベント内合計16000文字、走査256件、通常入力のネスト深さ10、抽出した例外項目のネスト深さ19までとする。深さ上限の超過は、その位置の値だけを `[limit]` に置換する。循環参照と非有限浮動小数はそれぞれ `[cycle]` / `[non-finite]` とし、共有参照は循環扱いしない。ネストした辞書に非文字列キーがあれば、その辞書全体を `[non-string-key]` にし値は見ない。
- 例外の `__str__` が失敗しても型と frame を残し、message は固定文にする。processor 自体の失敗は入力を含まない `log_policy_failed` に置き換え、業務側へ例外を伝播させない。原文 fallback や保護処理からの再帰ログはしない。
- ネスト内の未登録キーの allow 制御、自由文中の未知の秘密・本文の判別は別の保証であり、共通 deny の完全一致検査だけでは保証しない。

#### 入力上限と置換単位

文字列・合計文字数・走査数の上限と共有予算は `budget.py` の `LogEventBudget` が所有し、深さの上限は構造検査を行う `value_preparation.py` に定義する。frame数の上限は抽出を所有する `exceptions/extraction.py` に置く。定数だけの `limits.py` は設けない。単一文字列4000文字は、確認した診断文（典型的には数十〜数百文字、長い検証要約は約1700文字）に余裕を持たせた初期値である。イベント合計16000文字は、原因文・URL・frameなどを同時に残す余裕として採用した。2msという処理時間の仮目標から確定した値ではなく、本番の時間保証でもない。

| 制約 | 上限 | 超過時 |
| --- | ---: | --- |
| サニタイズ前の単一文字列・キー名 | 4000文字 | 文字列はその値だけを `[limit]` にする。キー名ならその項目を除外し、診断へ記録する。 |
| イベント内の保護対象文字数合計 | 16000文字 | ログ全体を共有予算超過の固定出力へ置換する（理由 `text_total`）。 |
| イベント内の走査数 | 256件 | ログ全体を共有予算超過の固定出力へ置換し、走査を止める（理由 `value_count`）。 |
| 通常入力のネストの深さ | 10 | 超過した位置の値だけを `[limit]` にする。 |
| 抽出した例外項目のネストの深さ | 19 | 超過した位置の値だけを `[limit]` にし、型検査・サニタイズ・マスクへ進まない。 |
| 例外frame数 | 50 | `frames` 全体を `[limit]` にし、末尾への切り詰めや残りの走査をしない。 |
| 整数のビット長 | 4096bit | 超過した整数だけを `[limit]` にする。 |

- processorは1つの `LogValuePreparer` に、通常入力には `DEPTH_LIMIT`、抽出した例外項目には `EXCEPTION_DEPTH_LIMIT` を呼び出し引数で渡す。上限は再帰処理へ引き継ぎ、インスタンスの設定は切り替えない。文字数・走査数の予算と診断情報はログ全体で共有する。例外用の19は、例外関係8段の`error_details.issues`にある`field`・`code`まで扱える辞書・配列の深さとして定義し、Pythonの例外探索の段数とは区別する。
- すべて上限ちょうどは保持し、超えた場合だけ置換する。文字数はPythonの文字列長で数え、UTF-8やJSONのバイト数ではない。
- 一項目の構造検査を完了してから、局所置換後に残った値をサニタイズする。置換した値の原文はサニタイズせず、秘密の断片も残さない。後続項目で共有予算を超えた場合には、先行項目の準備結果も出力せず破棄する。
- 単一長文・深さ・巨大整数の超過では、その位置だけ置換し正常な兄弟は残す。上限を超える文字列の原文は文字数予算へ加算しないが、残す文字列・キー名と実行済みの走査は計上し、巻き戻さない。共有予算の上限ちょうどで完了した場合は正常とし、追加の文字数・走査が必要なときに超過として処理を中止する。
- 合計文字数には、採用されたトップレベルの名前・文字列値・ネストのキーを含める。例外の `error_class` / `error_message` / `frames` / `error_details` / `causes` も通常項目と同じ入口で値を準備し、最後に入力由来の `_denied_keys` も同じ予算で処理する。同名項目は、SQLの実データや検証入力を除いた例外由来の値で上書きして優先する。固定のポリシー識別子・件数・マーカーはサニタイズ対象の文字数に加算しない。
- denyや未登録で除外した値は文字数検査もサニタイズもしない。キー正規化の前にはキー自体の長さを検査する。
- 走査数は深さと異なり、値・辞書・リストを一つずつ数える。例として単独の `[1, 2, 3]` はコンテナを含め4件。ネストのキーは値と同じ1項目として数えるが、文字数には加算する。
- トップレベルの項目を取り出すたび、項目名の検査より先に一件として数え、入力に含まれる内部制御・禁止・未登録・非文字列キーも256件の予算を消費する。ロガー属性のルールは入力に含まれず件数を消費しない。`log_item_count` はログ出力回数や最終フィールド数ではなく、この共有予算に計上した項目数を表し、上限は `MAX_ITEMS_PER_LOG_EVENT` とする。採用したトップレベル値は二重に数えず、例外の生成フィールドは値準備の前にまとめて計上し、ネストは各項目をたどる際に計上する。生成項目が残り予算を超える場合は加算せず中断する。入力の `exc_info` と変換で生成する各項目はそれぞれ計上する。例外の抽出自体は予算を持たず、値準備は呼び出し側の予算を使う。processor経由では通常値と同じ予算を使う。
- トップレベルとネストで共有する走査予算の129件目を確認した時点で超過を通知し、それ以降を列挙しない。各トップレベル項目の値を準備してから次の項目へ進むため、複数の上限を超える入力では、この順序で最初に検出した理由を返す。長すぎるキーはトップレベル・ネストとも名前や値を出さずその項目を除外し、`_policy_limited: true` で示す。
- SQLパラメータの抽出や例外の `__str__` は共通サニタイズ前の別工程であり、今回の入力上限でその抽出時間全体を保証しない。最終JSONの総バイト数の上限も別の契約である。

#### 値変換の責務

トップレベルの項目名の選別と、項目の値の構造検査・サニタイズを分ける。processorは `field_name` / `field_value` を一項目ずつ取り出し、計上 → `LogFieldSelector.select(field_name)` → 採用した名前の文字数計上 → `LogValuePreparer.prepare_field_value(field_value)` → 格納の順で処理する。不採用の値には触れず次へ進み、入力全体や選別結果の中間辞書は作らない。ネストした辞書では `key` / `child_value` と呼び、トップレベルのフィールドと区別する。

`LogFieldSelector` は型・内部項目・長さ・deny・allowを順に判定する。ネストした辞書の `inspect_dictionary` はdenyのみを判定し、トップレベルのallowを持ち込まない。どちらも共通の `normalize_key` と確定済みdenyを使い、禁止項目の値へ触れず除外する。選別側・構造検査側がそれぞれ同じ `LogProcessingDiagnostics` へ直接記録する。

例外の生成項目は通常入力の走査後に抽出し、まとめて計上して、一つずつ値を準備して格納する。生成項目は入力allowによる選別に混ぜず、確定済みの同じdeny・maskと共有予算で保護する。

`LogEventBudget` はログ一件ごとに作り、processor・値準備・診断が最初から共有する。トップレベル項目数と採用した項目名の文字数はprocessorが計上し、値準備は値の内部の検査と計上を担当する。`prepare_field_value` は呼び出し元の項目名を受け取らず、その項目を二重に数えない。単独利用の呼び出し側は外側の項目を先に計上する。診断は自身が生成する `_denied_keys` フィールドと各キー名を同じ予算へ計上し、出力準備を担当する。

| 処理 | 責務 |
| --- | --- |
| `LogFieldSelector.select` | トップレベルの項目名だけを検査し、採用するかを返す。denyをallowより先に判定し、未登録は名前を残さず件数だけ記録する。 |
| `LogPolicyProcessor` | 項目を順に計上・選別し、採用した項目名の文字数を数えて値の準備へつなぎ、返された値を格納する。例外と診断を合わせてログを組み立てる。 |
| `LogProcessingDiagnostics` | 各工程の診断を集約し、`prepare_log_fields()` で入力由来のキー名を共有予算で検査・サニタイズして出力用の診断を返す。`as_fields()` は記録内容の独立したスナップショットであり、通常のログ出力には使わない。 |
| `LogValuePreparer.prepare_field_value` | 一つの値を `inspect_value` で構造検査してから `prepare_text_values` でマスク・サニタイズし、準備した値を返す。項目名の選別・外側の項目の計上・出力辞書への格納・診断の出力準備は担当しない。 |
| `LogEventBudget.check_and_count_log_items` | 追加件数がログ一件の共有上限に収まることを確認してから `log_item_count` へ加算する。超過時は部分加算せず中断する。 |
| `LogEventBudget.check_and_count_text_chars` | 保護対象の文字数を共有カウンターへ加算し、合計文字数の超過時は中断する。 |
| `inspect_value` | 件数を計上済みの一つの値の深さ・型を検査し、子を持たない値は `inspect_scalar`、辞書は `inspect_dictionary`、配列は `inspect_sequence` へ渡す。循環判定は `active_container_ids` で現在の経路だけを見る。 |
| 項目の計上位置 | 通常入力は選別前、例外などの生成項目は値準備へ渡す前、ネストした辞書・配列は各項目の検査前に計上する。値準備ではトップレベル項目を再加算しない。 |
| `inspect_scalar` | `None`・真偽値はそのまま返し、巨大整数・非有限浮動小数・単一上限を超える文字列は固定マーカーへ置換する。文字列は単一上限を通過した文字数を合計へ加算し、全体上限の超過時はログの準備を中断する。 |
| `inspect_dictionary` | 非文字列キーがあれば辞書全体を `[non-string-key]` にし値は見ない。文字列キーだけのとき、一項目ごとに検査件数・キー長・deny・合計文字数を確認し、子の値を `inspect_value` で検査して新しい辞書へ格納する。 |
| `inspect_sequence` | 配列をたどり、各要素の件数を確認・加算し、子の値を `inspect_value` で検査して順序を保った新しい配列へ格納する。 |
| `prepare_text_values` | 検査済みの組み込み型の構造だけを辿り、文字列値とネストのキーへ `sanitize_text` → 同じ `mask` による `mask_assignments` を一度ずつ直接適用する。トップレベルの項目名は置換しない。 |
| `extract_exception_fields` | 受けてよい `exc_info` は structlog と同じ3形式（`True` / 例外 / 3要素 tuple）とし、合ったものだけ `ExcInfo` に揃える。原因文は型を見て `extract_sql_error_message` / `extract_validation_message` / `str(exc)` のどれかから一度取り、型名とframeを合わせて `ExceptionLogFields` を作る。SQL例外では原因文と独立した `extract_sql_error_details` から `error_details` を追加し、通常例外のcause/contextを同じ構造の `causes` へ、グループのメンバーを `exceptions` へ展開する。直接のdriver例外は原文を省略する。合わない値はフィールドを作らない。伏せ字と上限は値準備が担当する。processorは通常入力の走査後に生成項目を抽出し、通常項目と同じ入口で各値を準備する。 |

検査で作る構造は入力と分離し、入力は変更しない。値の置換(`[limit]`・`[unsupported]`・`[cycle]`・`[non-finite]`・`[non-string-key]`)は内部マーカーで表し、サニタイズ・マスクを通さず固定文字列として出力する。`LogBudgetExceeded` は共有予算超過をprocessorへ通知する。単独の値準備・例外準備ではこの通知を呼び出し側へ伝播し、processorではログ全体を固定出力へ置換して業務側へ伝播させない。

processorは各 `preparer.prepare_field_value(...)` の結果を格納した辞書へ `diagnostics.prepare_log_fields(...)` の結果を結合し、診断のフィールド名や構造には立ち入らない。診断はキー名の一覧全体の予算検査を終えてからサニタイズし、長すぎるキー名は原文を処理せず `[limit]` にする。診断の準備中に共有予算を超過した場合も、通常項目を含むログ全体を固定出力へ置換する。

入力からログ出力までの上限・共有予算の境界は `test_output_limits.py` が担当する。`TestLocalReplacement` で単一文字列・キー名・整数・深さ・例外由来の値の超過した位置だけを置換し正常な兄弟を残すことを、`TestWholeLogReplacementByTextBudget` / `TestWholeLogReplacementByItemBudget` で共有予算の上限ちょうどの保持と超過時のログ全体の置換を、processor経由で確認する。不正キー・内部項目・生成した例外項目・例外frameも実際の入力へ含め、通常項目との予算共有を確認する。禁止したネスト項目の件数、辞書と配列の混在、複数項目にまたがる予算共有も、上限ちょうどと一件超過の別テストで確認する。複数の予算を同時に超えたときの理由は `TestBudgetOverflowReason` が担当する。例外frame数の上限は抽出側が所有するため `exceptions/test_extraction.py` が担当する。processorの走査停止と状態分離は `test_processor.py`、超過した値をサニタイズへ渡さない処理内部の保証は `test_value_conversion.py`、置換後のマーカーと固定出力がrendererで戻らないことは `test_chain.py` が担当する。境界の異なる条件は独立したテストにし、予算をテスト側で直接加算するだけのケースを出力保証として扱わない。

`test_processor.py` の `TestExceptionValueDepthLimit` は、同じログ内の通常入力と例外項目に異なる深さ上限を適用し、上限内のframeを保持して上限直後の値を置換することを確認する。`test_value_conversion.py`は例外用の値準備上限ちょうどの保持と一段超過の検査停止を、`exceptions/test_application_output.py`は探索の最深部の`field`・`code`が最終ログへ残ることを確認する。

`test_budget.py` は `TestItemAccounting` / `TestTextAccounting` で計上と超過通知の単体契約を確認し、まとめた件数が超過したときに部分加算しない保証も保持する。診断の記録・集計・出力準備・返却値の分離は `test_diagnostics.py`、項目名の検査・deny優先・allow判定は `test_field_selection.py`、一項目ずつの処理順・不採用値を検査しないこと・実チェーンとの接続は `test_processor.py`、循環・共有参照・予約キー・独自型は `test_base_guards.py`、処理失敗時の固定出力と保護処理からの再帰ログの禁止は `test_processor.py` が担当する。`test_value_conversion.py` は辞書の操作・型変換・入力非変更・診断の独立性・文字列の処理回数を確認し、検査前の件数計上、不正な辞書の中身を検査しないこと、予算超過した項目の値やキーを処理しないことは呼び出しの記録で確認する。トップレベル項目の二重計上は `test_output_limits.py` の上限件数ちょうどの出力テストに集約する。

## 適用位置

- API / worker / Lambda 共通の structlog 構成を本パッケージが持ち、processor を renderer の直前に置く。既存の `setup_logfire` (`logfire.StructlogProcessor` を含む) と `setup_lambda_logging` は、組み込み時にこの構成へ置き換える。
- 順序: deny除外 → 未登録除外 → 一項目の構造検査と局所置換 → 残った文字列値・ネストのキー名のsanitize → mask。共有予算超過はどの工程でもログ全体の固定出力へ切り替える。切り詰めは行わない。
- 例外は `format_exc_info` の代わりに基底が構造化する。processor が `exc_info` を消費し、型 FQN、規則 3 を通した message、frame metadata (file / function / line。locals・ソース行は含めない) に置き換える。renderer には `exc_info` が届かないため、dev console も traceback 文字列を出さない。通常のcause/contextは基底で構造化し、SDK固有の原因構造は目的ポリシー実装時に対応する。
- 例外文・型FQN・frameのファイル名と関数名には、選択したポリシーの同じ `mask` と内容検出を適用する。型名とframeに共通maskのみを暗黙適用しない。
- 配置は `backend/app/log_policy/`。既存チェーンの置き換えは、目的ポリシーの定義と `policy_logger` への移行を終えてから行う (置き換え時点で未宣言 logger の全フィールドが落ちるため)。

## Non-goals

- AI推論のモデル・トークン数以外の目的別allow一覧とmoduleとの対応 (別文書)。
- 利用者テキスト (question / previous turn) の禁止。利用者対話ポリシー側で定める。
- 任意の自由文の意味解析、未知の例外形式からの入力値の完全抽出。traceback の locals は常に除外する。
- public / restricted の profile 引数、Logfire の trace / span 側の変更、frontend、監査 DB `error_message` の契約変更。
- 既存ログの再加工、`title` の扱い、構造化項目の値全体をmaskで置換する機能。

## Verification

合成値のみを使い、実 credential・実ログを使わない。

| ID | 条件 | 期待結果 |
| --- | --- | --- |
| B01 | 禁止キーを bind / contextvars / 引数から渡す | 3 経路すべてでキーが出ず `_denied_keys` に名前だけ残る。 |
| B02 | 許可値の文中に AWS secret key / DSN userinfo / Bearer token | 値全体が置換され、他の文は残る。 |
| B03 | AWS ARN / RDS endpoint を許可キーで渡す | 基底では置換しない (識別情報は目的ポリシーの判断)。 |
| B04 | 仮の deny・mask を明示し、ネスト dict・list・既知キー付き例外文を渡す | 指定した禁止値を除外し、許可した計量値は残る。実際のAI・記事取得の禁止項目の妥当性は別途検証する。 |
| B05 | SQL / parameters 連結、DETAIL、primary message 内の bind 値 | 対象値を除外し、型・SQLSTATE・保護後の primary message・frame が残る。 |
| B06 | ポリシー未宣言 logger に任意キー | 業務キー除外、`_unregistered_count` に件数のみ。 |
| B06a | 目的ポリシーの allow に基底 deny のキーを含めて定義 | 定義時に失敗し、processor に到達しない。 |
| B06b | allow に `completion_tokens`、deny に `token` | 完全一致なので `completion_tokens` は出る。 |
| B07 | 上限超の長文末尾に secret | その文字列を `[limit]` にし、原文のサニタイズを実行せず断片も残さない。 |
| B08 | production JSON と dev console | 秘匿結果が一致し、整形で原文が復活しない。 |
| B09 | 予約キー、独自オブジェクト、循環・深い・大量の値、処理失敗 | 迂回や原文 fallback がなく、保護処理で業務を落とさない。 |

所在テスト ([backend/tests/log_policy/](../../backend/tests/log_policy/)):

- B01 / B03 / B04 / B06 / 文字列によるポリシー指定の拒否 / event の sanitize: `test_processor.py` (実チェーン + `LogCapture` で検証。`capture_logs` は configured processors を差し替えるため使わない)
- B02 / 通常テキストの保持: `test_sanitize.py`・`test_mask.py`・`test_text_preparation.py`
- B07 / 通常値・例外文・型名・frameの出力上限: `test_output_limits.py`
- `exc_info`の解決、cause/context・グループの共通探索、循環、深さ・総数・frame上限、集約済み原因の探索停止: `exceptions/test_extraction.py`
- B05 / SQL原因文の変換: `exceptions/test_sql_conversion.py`、生のValidationErrorの保護: `exceptions/test_safe_exception_log.py`
- アプリの4種類の検証例外の診断変換、対象外の委譲、変換失敗時の保護: `exceptions/test_application_conversion.py`
- 検証境界からJSONまでの診断保持、最深部の項目、共有予算: `exceptions/test_application_output.py`
- SQL診断の許可属性・取得失敗・原因文との独立性、SQL内部の集約と親子の診断分離: `exceptions/test_sql_details.py`
- 不正な`exc_info`の生値を出力しないこと: `test_processor.py`
- JSON出力の保護・診断辞書の注入防止: `exceptions/test_sql_output.py`
- 実DBでの一意制約・NOT NULL違反と製品セッション境界からの診断出力: `exceptions/test_sql_diagnostics_integration.py`
- B06a / キー正規化 / deny・maskの独立性と継承・親の非変更・allowの明示: `test_base.py`
- B06b / 登録済み規則の適用・未登録時の制限: `test_processor.py`
- 認証キーの表記揺れ、継承済みdenyの構造化項目への伝達とmaskの文字列・例外への伝達: `test_policy_boundaries.py`
- B01 / B02 / B04 / B05 / B09 の回帰条件: `test_base_guards.py`
- B08: `test_chain.py` (`JSONRenderer` と `ConsoleRenderer` を `build_processors` で通し、宣言後の共通出力設定も反映する)
- ルールの保持・生成時の型確認・遅延生成・キャッシュ・入力経路からのルール変更防止: `test_logger.py`

共通deny強化時の検証 (2026-09-17、責務分離前):

- `uv run ruff check app/ tests/log_policy` / `uv run ruff format --check app/ tests/log_policy`: 成功。
- `uv run pytest tests/ -m unit -x -q`: 7017 passed、1369 deselected。DB テストは下記の専用環境で実行する。
- 追加の予約値・末尾エスケープ回帰条件を含む最終版: `uv run pytest tests/log_policy --confcutdir=tests/log_policy -q`: 102 passed。
- `make test-integration`（専用 project、Compose は `--env-file /dev/null`）: 1369 passed。テスト用 DB / Redis と network の削除まで完了。
- 全体テストは作業用の起動スクリプトでルート `.env` の読込を無効にし、合成設定のみを注入した。製品の設定・テスト fixture は変更していない。

既存チェーンは未置換のため、実プロセスの stdout での確認は組み込み step で行う。

## Implementation

- [backend/app/log_policy/base.py](../../backend/app/log_policy/base.py): `LogPolicy`、基底allow・deny・mask、`BASE_LOG_RULES`、`normalize_key`、`LogPolicyRules` (生成時のallow・deny・mask確定と継承)
- [backend/app/log_policy/sanitize.py](../../backend/app/log_policy/sanitize.py): S1/S2 パターン集、`sanitize_text`（内容から秘密情報を検出する）。長さ制限は出力側
- [backend/app/log_policy/mask.py](../../backend/app/log_policy/mask.py): `mask_assignments`（指定キーに対応する文字列内の値全体を伏せる）
- [backend/app/log_policy/exceptions/](../../backend/app/log_policy/exceptions/): 種類ごとの変換。`sql.py` は原因文の保護とSQL診断、`validation.py` はPydantic検証、`event_validation.py` はイベント検証、`application.py` は共通アプリケーション例外を担当し、すべて `ConvertedException` を返す
- [backend/app/log_policy/exceptions/extraction.py](../../backend/app/log_policy/exceptions/extraction.py): `exc_info` の解決と `ExceptionLogFields` の組み立て。型は型名・原因文・frameと任意の `error_details` / `causes` / `exceptions` を持つ最終出力の契約とする。原因ノードにも同じ型を使う。伏せ字と上限は値準備が担当する
- [backend/app/log_policy/exceptions/conversion.py](../../backend/app/log_policy/exceptions/conversion.py): 例外1件の種類別変換を担当し、原因文・診断属性・内部原因の集約状態を返す。SQL内部の診断抽出は `sql.py` に委譲し、通常の原因連鎖とグループの探索は `extraction.py` が担当する
- [backend/app/log_policy/exceptions/application.py](../../backend/app/log_policy/exceptions/application.py): `ApplicationError`のメッセージと明示された`details`を共通形式へ写す。例外の任意属性を自動展開せず、共通入口から呼び出す
- [backend/app/log_policy/policies/ai_inference.py](../../backend/app/log_policy/policies/ai_inference.py)、[external_content.py](../../backend/app/log_policy/policies/external_content.py): 目的別定義（AI推論はモデル・トークン数の完成済みルールも定義）
- [backend/app/log_policy/diagnostics.py](../../backend/app/log_policy/diagnostics.py): ログ一件の診断の記録・集計・出力形式への変換
- [backend/app/log_policy/field_selection.py](../../backend/app/log_policy/field_selection.py): トップレベルの項目名の検査とdeny・allow判定
- [backend/app/log_policy/value_preparation.py](../../backend/app/log_policy/value_preparation.py): 選別済みの値をログに使える状態へ準備する処理、構造検査とサニタイズ、共有予算
- [backend/app/log_policy/bound_logger.py](../../backend/app/log_policy/bound_logger.py): `wrapper_class`に指定するINFO以上の共通ラッパー。通常のログメソッドからprocessor・整形・出力までの例外を捕捉し、再記録しない。`bind()`後も同じ保護を維持し、プロセス中断は抑止しない。位置引数の文字列展開は保護範囲外。検証は`test_bound_logger.py`に集約する
- [backend/app/log_policy/processor.py](../../backend/app/log_policy/processor.py): 完成済みルールの受け取り、一項目ずつの計上・選別・値準備・格納、例外と診断の接続、固定の失敗イベント
- [backend/app/log_policy/logger.py](../../backend/app/log_policy/logger.py): `PolicyLogger`、`create_policy_logger`、structlogの遅延生成を使う `policy_logger`
- [backend/app/log_policy/chain.py](../../backend/app/log_policy/chain.py): `build_processors` (既存チェーンへの組み込みは未実施)

## Done

- [x] 共通の認証情報deny・maskと目的別の本文deny・maskを定義時に確定し、processorが完成済みの規則を適用できる。
- [ ] API / worker / Lambda のチェーンを `build_processors` へ置き換える (目的ポリシーと `policy_logger` 移行の完了後)。
- [x] B01〜B09 の上記限定された保証範囲が合成値で通る。
- [ ] Evidence の生 `str(exc)` 箇所が基底を通した出力になっている。
- [x] 目的ポリシーの追加が、基底の変更なしに allow の明示・deny / mask の追加で可能である。

以下の日付付き節は当時の実装・検証の記録であり、現在の構成と契約は上記を正とする。

## 今回の修正範囲（2026-09-17）

共通 deny の別名不足、予約キー・独自型による迂回、短い認証値や引用符内の部分漏れ、既知の例外形式の入力混入、循環や文字列化失敗を修正する。目的ポリシーの allow 定義、ポリシー選択を不変にする仕組み、既存プロセスのチェーン置換は含めない。完了条件は、この範囲の回帰テストと仕様が一致し、対象の検証結果を記録すること。

一次資料: [Pydantic の例外情報](https://docs.pydantic.dev/latest/errors/errors/)、[SQLAlchemy StatementError](https://docs.sqlalchemy.org/en/20/core/exceptions.html#sqlalchemy.exc.StatementError)。

## 責務分離の追加修正（2026-09-17）

Problem: 本文・派生テキストが全目的で禁止され、基盤接続の説明まで落ちる。共通 deny にアプリ固有の認証情報名が不足している。
Evidence: `rules.py` / `sanitize.py` / `processor.py` / `safe_exception_log.py`、`app/config.py` / `app/db/settings.py` の設定名と契約テストを確認した。
Invariants: 認証情報は全目的で禁止、本文は取得・AI推論の目的別禁止、未登録項目は出力しない。追加 allow で各禁止を上書きできない。
Non-goals: 目的別 allow 一覧の策定、既存 structlog チェーンの置換、既存ログ呼び出しの移行。
Done: 設定名の明示 deny、本文の責任分離、文字列・例外文への選択目的の適用を実装し、契約テストと全体検証を通す。

今回の検証:

- `uv run ruff check app/ tests/log_policy` / `uv run ruff format --check app/ tests/log_policy`: 成功。
- `uv run pytest tests/log_policy --confcutdir=tests/log_policy -q`: 183 passed。
- `uv run pytest tests/ -m unit -x -q`: 7102 passed、1369 deselected。
- 全体テストは前回と同じ `.env` を読まない検証用起動スクリプトと合成設定で実行した。
- `make test-integration`（専用 project、Compose は `--env-file /dev/null`）: 1369 passed。テスト用 DB / Redis・network の削除まで完了。

## 適用処理の責務分離（2026-09-17）

Problem: `rules.py` が具体的な本文禁止と目的別マッピングを持ち、processor が選別・値変換・ポリシー解決を兼ねていた。
Evidence: 共通規則・processor・チェーン・既存183件の契約テストを確認した。
Invariants: 共通・目的別 deny 優先、未登録除外、例外とネストの保護、処理失敗時の固定イベント、出力内容を維持する。
Non-goals: 新規 allow 項目の策定、既存アプリのログ設定への接続、新しい出力先や依存の追加。
Done: 各責務を分離し、単独の契約テストと既存チェーンのテスト、バックエンド検証が通ること。

`LogPolicyRules` は共通 deny と渡された deny だけを合成し、目的名から業務規則を推測しない。`PolicyRegistry` が明示登録された定義に目的別 deny を合成する。目的別定義ファイルの存在だけでは有効化せず、未登録・文字列の識別子は従来どおり共通規則のみとする。

選別は値を加工せず参照を渡す。値保護はその値を変更せずに安全な型へ投影する。ネストの deny 判定には同じ有効 deny を使う。processor は各処理を順に呼び出し、JSON エンコードは引き続き renderer が担う。

単独の境界検証は `test_field_selection.py`、チェーンを通す既存の出力検証は従来のテストが所有する。

責務分離後の検証:

- `uv run ruff check app/ tests/log_policy` / `uv run ruff format --check app/ tests/log_policy`: 成功。
- `uv run pytest tests/log_policy --confcutdir=tests/log_policy -q`: 189 passed。
- `uv run pytest tests/ -m unit -x -q`: 7108 passed、1369 deselected。
- `make test-integration`: 1369 passed。専用 DB / Redis と network の削除まで完了。
- 全体テストは前回と同じ `.env` を読まない検証用起動スクリプトと合成設定で実行した。

## 共通基盤のテスト範囲の整理（2026-09-17）

AI・記事取得の具体的な本文禁止一覧を固定するテストは、目的別ポリシーの正式定義時まで対象外とする。目的別内容の境界テスト7関数（27ケース）を削除した。共通の登録・合成・ネスト保護は仮の禁止項目を明示し、実際の本文一覧に依存せず検証する。登録済み deny と allow の競合、例外文への追加 deny の伝達も仮の定義で確認する。

今回の変更はテストと検証範囲の記録のみ。`policies/` の暫定定義は変更しておらず、その具体的内容を確定・検証済みとは扱わない。AIポリシーの正式定義は次の作業とする。

認証情報の共通 deny は目的名に依存しないため、キー名ごとのケースを残して目的名5種類との直積を外した（重複40ケースを削減）。

テスト整理の検証: lint / format 成功。目的別ケース削除直後の全体単体テストは7081 passed。その後の共通契約への置換・直積削減を含む最終の対象テストは124 passed。DB結合テストは1369 passed、専用コンテナとnetworkの削除まで完了。設定の隔離は前回と同じ検証用起動スクリプトを使用した。

## 例外抽出と共通サニタイズの整理（2026-09-17）

以下は当時の作業記録。500文字切り詰めは、後述の入力上限導入で廃止した。

Problem: 例外の型別抽出と文字列保護・出力組み立てが混在し、対象と保護規則の対応を追いにくい。
Evidence: `sanitize.py` / `safe_exception_log.py` / `value_preparation.py` の全呼び出しと既存の契約テストを確認した。
Invariants: 既存の禁止値除外、SQL・入力検証例外の抽出結果、失敗時の固定文、型名・frame・通常値の長さ上限を維持する。秘匿してから切り詰める。
Non-goals: AIポリシーの定義、禁止項目・抽出内容の変更、既存アプリのログ設定への接続。
Done: 型別の抽出先と適用順序を明示し、最終出力の回帰テストとバックエンド検証を通す。

- `exception_messages.extract_exception_message`: SQL例外 → `extract_sql_error_message`、入力検証例外 → `extract_validation_error_summary`、その他 → 通常の原因文。共通の認証情報置換や文字数制限は行わない。
- `sanitize.sanitize_text`: 共通認証情報と指定された禁止キー付き値を置換する。文字数制限は行わない。
- `safe_exception_log.build_exception_log_fields`: 対象固有の抽出後、共通文字列保護、500文字制限、フィールド組み立てを行う。SQLSTATEの取得失敗時の固定文も維持する。
- `ExceptionLogFrame`: 引き続き `file` / `function` / `line` の出力形。`ExceptionLogFields`: `SafeExceptionLogFields`から改名した出力形。型そのものが安全性を検証するという保証は持たせず、生成処理が保護を担う。
- `LogValuePreparer`: 通常値とネストしたキー名を秘匿してから500文字に制限する。

文字数制限の既存テストは `test_output_limits.py` へ移し、例外文・型名・frameについても出力側の秘匿順序と上限を検証する。`test_sanitize.py` は置換のみの契約を検証する。

今回の検証:

- lint / format / `git diff --check`: 成功。
- 対象テスト: 120 passed。バックエンド単体テスト: 7039 passed、1369 deselected。
- `make test-integration`: 初回はRedisの `begin_attempt` が `None` を返すケースで1件失敗（4 passed）。元の例外が記録されていないため原因は未確定。コード・テスト・設定を変更せず同条件で再実行し、1369 passed。専用DB / Redisコンテナとnetworkの削除まで完了。
- 全体テストは `.env` を読まない検証用起動スクリプトと合成設定で実行した。Composeには専用projectと `--env-file /dev/null` を指定した。


## 基底・継承とテストの整理（2026-09-18）

Problem: 規則生成とRegistry登録の二段階でdenyを合成しており、定義だけでは適用される禁止項目が確定しない。
Evidence: 規則・目的別定義・processor・保護処理の呼び出しと、ログポリシーの契約テストを確認した。
Invariants: 共通・親・子のdenyを解除できず、入力・親の規則を変更しない。既存の出力形、保護規則、未登録時の制限を維持する。
Non-goals: 目的別allowの策定、禁止項目・サニタイズ規則・MASK機能の変更、実アプリのチェーン組み込み。
Done: 規則生成時にdenyが確定し、Registryを介さず適用でき、契約テストとバックエンド検証が通ること。

- `base.py` が共通規則・生成時の検証・継承を所有し、`rules.py` と `registry.py` は廃止した。
- `test_base.py` に定義・継承とアプリ固有の認証キーの禁止を集約し、Registryのテストを置き換えた。
- 継承した同じdenyが構造化項目・ネスト・文字列へ届く接続テストを残し、重複するevent文字列だけのテストを削除した。
- processor経由のARN・endpoint保持テストを削除し、対象別・共通サニタイズのテストに保証を残した。
- 業務ポリシーの具体的なallowと実ログ呼び出しは、それらを導入する作業で検証する。

今回の検証:

- lint / format / `git diff --check`: 成功。
- ログポリシーの対象テスト: 203 passed。バックエンド単体テスト: 7122 passed。
- `make test-integration`: 1369 passed。専用DB / Redisコンテナとnetworkの削除まで完了。
- 全体テストはリポジトリ外の検証用起動スクリプトでdotenv読込を無効にし、合成設定を使用した。Composeは `--env-file /dev/null` を指定した。


## 入力上限と項目単位の置換（2026-09-18）

Problem: 全文サニタイズ後の切り詰めでは負荷を止められず、上限超過した辞書・リストの部分結果が正常な値に見える。
Evidence: 前回の性能測定、通常の診断文の長さ、既存の値変換・選別・例外抽出と境界テストを確認した。
Invariants: denyを先に適用し、超過したトップレベル項目はサニタイズせず全体置換する。上限内のサニタイズ規則、入力非変更、循環・独自型の保護を維持する。
Non-goals: 実アプリのログ設定、目的別allow、MASK、DB・API・依存関係の変更、実ログの頻度やJSON配送サイズの保証。
Done: 単一4000文字・合計16000文字・走査128件と項目単位の置換を実装し、関連する境界・接続テストが通ること。

検証: ログポリシーの関連テスト251件成功。バックエンドappと関連テスト・測定スクリプトのlint・format、`git diff --check` 成功。測定スクリプトも実行し、URL/JWTの旧検出結果との50400ケースの一致を維持した。測定JSONには現在の上限とpayloadの上限置換有無を記録する。追加変更を先に行うというユーザー指示により、全体単体テスト・DB結合テストの再実行は保留した。前回の全体成功結果は今回の変更後の検証結果として扱わない。


### 制限値の定義場所の修正

数字だけの `limits.py` を廃止し、単一文字列・合計文字数・走査数・深さは `LogValuePreparer` と同じファイルに置いた。processorは先にイベント用の `LogValuePreparer` を作り、選別へ件数・文字列上限を渡す。選別で検査した件数を同じ残り予算から引き、通常値・例外・診断の処理へ引き継ぐ。選別側に別の既定値や定数の別名は持たせない。frame数は引き続き例外抽出側が所有する。数値・出力結果・予算の数え方は変更しない。


## 完成済みルールの受け渡し（2026-09-18）

`BASE_LOG_RULES` と目的別ルールを同じ `policy_logger()` からbindし、processorは確定済みのallow・denyを適用する。`build_processors(renderer)` は規則の登録を受け取らない。

allowは入力フィールドの採用規則とする。保護済み例外情報と固定の診断情報は選別後に生成する既存契約を維持し、入力allowには自動追加しない。生成項目のトップレベル名を目的別denyで除外する変更は含めない。通常値・例外・入力由来の診断は同じ `LogValuePreparer` のdenyと予算を共有する。

関連テストと性能測定スクリプトの呼び出しを新APIへ更新した。ユーザー指定により、この変更の単体・結合テストと性能測定は実行しない。API・worker・Lambdaへの実チェーン組み込みは引き続き別作業とする。

静的検証: `uv run --no-sync ruff check app/ tests/log_policy/ scripts/benchmark_log_sanitization.py` と同範囲の `ruff format --check` は成功。


## フィールド選別と値の準備の責任整理（2026-09-18）

Problem: トップレベルのdeny・allowが一つの選別処理に混在し、ネスト内にもdeny判定が直接書かれていた。
Evidence: 現在の呼び出しと仕様を確認し、変更前の関連テスト264件が成功した。
Invariants: deny優先、allowはトップレベルのみ、遅延走査、値の参照維持、共有予算、項目単位の全体置換、基底型契約と既存の出力結果を維持する。
Non-goals: 公開API、実アプリのログ設定、目的別allow、サニタイズ規則、DB・API・依存関係の変更。
Done: processorから三工程と値の準備の順序を読め、関連テストと静的チェックが成功すること。

- `fields.py` に三工程と `FieldDiagnostics` を置き、選択結果を診断情報から分離した。
- `value_preparation.py` の `LogValuePreparer.prepare_selected_fields` を値の準備の入口とし、ネストも共通deny除外を呼ぶ。
- 例外処理は同じ `preparer` を受け取り、通常値と同じ予算を共有する。生成したトップレベル項目を新たにallow・deny選別へ通さない。
- 旧モジュール・クラス・メソッドの互換用別名は置かず、テストと測定スクリプトの参照を移行した。過去の測定JSONは変更しない。
- 選別テスト6ケースを責任別の直接テストと接続テストへ移し、異なる境界条件を維持した。

今回の検証: 関連テスト274件成功。バックエンドapp・関連テスト・測定スクリプトのlint / format、旧名・旧importの参照確認、測定スクリプトのimport確認、`git diff --check` が成功した。ユーザー指定により全体単体テストと `make test-integration` は再実行していない。


## ログ一件分の診断記録の責任整理（2026-09-18）

Problem: フィールド検査による診断属性の直接更新と、値の準備処理によるネスト除外の集計が分散していた。

Evidence: フィールド検査・processor・値の準備処理と、既存の関連テスト274件を確認した。

Invariants: 診断の出力名・内容・禁止キー順序と重複、走査範囲・予算消費、入力非変更、既存の上限と置換結果を維持する。

Non-goals: 新しい診断項目、実アプリのログ設定、allow／denyの規則、サニタイズ、公開API、DB・依存関係は変更しない。

Done: 診断の更新を専用メソッドに集約し、検査件数を診断から分離して、関連テストと静的チェックが成功する。

processorは、ルール取得 → 診断生成 → 同じ診断を持つpreparer生成 → 入力検査 → deny除外 → allow選別 → 検査件数を共有予算から控除 → 選択した値の準備 → 同じpreparerで例外準備 → 診断の取り出しと禁止キー名の準備 → 出力の組み立て、の順で進める。

- 診断の非公開状態は専用メソッドだけで更新し、未登録名や除外した値は保存しない。
- ネストの禁止キーと非文字列キーは別の記録メソッドを呼び、既存の `_denied_nested_count` へ合算する。
- 入力検査の `visited_entry_count` は読み取り用プロパティで取得し、選別後に共有予算へ一度だけ反映する。
- 単独のpreparerは自身専用の診断を生成し、processor経由ではログ一件の診断を共有する。
- `_denied_keys` は内部リストのコピーを取り出し、通常値・例外と同じ予算とdenyで準備してから出力する。
- 値の上限超過は既存の `[limit]` 置換を維持し、新たな診断集計は追加しない。

診断の単体契約は `test_diagnostics.py`、トップレベルとネストの記録の合流・ログ間の状態分離・禁止キー名のサニタイズは `test_processor.py`、単独preparerの状態分離は `test_value_conversion.py` が担当する。既存の入力境界と予算・走査停止のテストは移行して維持した。

今回の検証: 関連テスト288件成功。バックエンドapp・関連テスト・測定スクリプトのlint、formatチェック（675ファイル）、実装・テスト・測定スクリプトの旧名参照確認、git diff --checkが成功した。全体単体テストとmake test-integrationはユーザー指示により再実行していない。


## 値の準備処理の診断出力の統一（2026-09-18）

Problem: 値の準備処理が項目数超過・非文字列キー・キー長超過の診断を出力辞書へ直接書いていた。

Evidence: 診断出力の全参照と例外経路を確認し、通常経路の直接書き込みが値の準備処理の2箇所に残っていることを確認した。

Invariants: 打ち切り・除外の条件、後続項目の扱い、予算消費、processorの最終診断出力、値の固定マーカー置換を維持する。

Non-goals: 値の上限超過などに新しい診断を追加せず、処理全体が失敗した場合の固定ログも変更しない。

Done: 値の準備処理が専用メソッドで診断を通知し、診断値の準備中に起きた記録も最終出力へ反映して、関連テストと静的チェックが成功する。

- 項目数とキー長の超過は `record_preparation_limit_reached()`、非文字列キーの除外は `record_preparation_invalid_key()` で記録し、どちらも既存の `_policy_limited` に反映する。
- `prepare_selected_fields()` の返り値には準備した値だけを保持する。単独利用時の診断は `preparer.diagnostics.as_fields()` から取得する。
- processorは入力由来の禁止キー名を準備した後に診断を取り直し、未処理の禁止キー名を除いて準備結果と合わせる。診断項目の準備で項目が落ちた場合も原文を復元しない。
- `[limit]`・`[unsupported]`・`[cycle]` などの値の置換は処理側に残し、処理失敗時の `_policy_error` は診断機構に依存しない固定出力を維持する。

記録メソッドの契約は `test_diagnostics.py`、項目数・キー型・キー長の各通知経路は `test_output_limits.py`、診断値の準備中の記録反映と原文を戻さない契約は `test_processor.py` が担当する。

今回の検証: 関連テスト295件成功。lint、formatチェック（675ファイル）、診断の直接書き込み参照確認、git diff --checkが成功した。ユーザー指示により全体単体テストとmake test-integrationは保留した。


## 個別の制限と共有予算超過の分離（2026-09-18）

Problem: 局所的な値の超過とログ全体の処理量超過を同じ通知で扱い、トップレベル項目の全体置換と文字数予算の巻き戻しが混在していた。

Evidence: 値の準備・入力検査・processor・例外準備、診断と測定スクリプト、既存295件の関連テストを確認した。

Invariants: deny優先・allowはトップレベルのみ、秘密の原文を出さないこと、型契約、入力非変更、循環・独自型への対処、上限値、上限ちょうどの正常処理、遅延走査を維持する。

Non-goals: サニタイズ規則・実アプリへの組み込み・DB・依存関係・公開APIの変更は行わない。全体単体テストとintegrationの再実行は保留する。

Done: 局所置換とログ全体の固定出力を分離し、一項目の準備と予算・計上済み状態の名前を整理して関連テストと静的チェックが成功する。

共有予算超過時の出力は次の3項目のみとし、元のevent・値・キー名・ポリシー識別子・途中の診断は含めない。

```json
{
  "event": "log_policy_budget_exceeded",
  "_policy_limited": true,
  "_policy_limit_reason": "value_count"
}
```

合計文字数の超過では理由を `text_total` とする。各処理は `LogBudgetExceeded` を通知し、processorが診断へ記録して固定出力を返す。共有予算は残り数 `remaining_value_count` / `remaining_text_chars` で扱い、超過後の継続処理や巻き戻しは行わない。前段での計上済み状態は `fields_already_counted` / `value_already_counted` で明示する。

局所的な長文・巨大整数・深さ超過では正常な兄弟を保持し、frameの長いfile/functionもその値だけを置換する。frame件数の制限は従来どおりframes全体の置換とする。長すぎる辞書キーは項目を除外し診断へ記録する。値準備の入口で除外したキーも走査件数に含める。

既存の境界テストは新しい期待結果へ移行し、共有予算超過の固定出力、正常な兄弟の保持と秘匿、診断処理中の予算超過、連続ログの状態分離、JSON／console、診断失敗時の固定エラーを検証する。測定スクリプトは出力結果を `budget_limit_reason` と `payload_retained` で記録する。過去の測定JSONは変更しない。

今回の検証: 関連テスト308件成功。バックエンドapp・関連テスト・測定スクリプトのlint、formatチェック（675ファイル）、旧名参照確認、測定スクリプトのimport確認、git diff --checkが成功した。全体単体テストとmake test-integrationはユーザー指示により保留した。


## 基本ログ項目の型強制と薄いヘルパーの廃止（2026-09-18）

Problem: 基本5項目だけの型強制と、一項目の前提条件が呼び出し元へ分散したヘルパーが値準備の流れを読みにくくしていた。

Evidence: 導入済みstructlogのrendererと、基本5項目へ数値・辞書・リスト・None・真偽値を渡した試行を確認した。JSONは全項目を出力でき、consoleのlevelだけ文字列を要求するが、通常のチェーン前段で生成される。仕様・テスト以外の利用側に5項目の一律型強制への依存は見つからなかった。

Invariants: 基本allowのキー集合、通常の型・構造検査、deny除外、サニタイズ、局所置換、共有予算超過の固定出力、入力非変更を維持する。

Non-goals: 実アプリのログ構成、rendererの独自実装、目的別allow・deny、サニタイズ規則は変更しない。

Done: 基本項目を通常の値検査へ統一し、一項目の処理順を同じ場所で読める構成にして、関連テストと静的チェックが成功する。

型の対応表と参照関数を廃止し、基本allowを直接定義する。薄い一項目用ヘルパーを廃止して、キーの文字数計上 → 値の構造検査 → サニタイズ → 格納をフィールドの準備処理にまとめる。キーは型・長さの検査後に文字数を計上し、文字列値は単一上限検査後に同じ文字数予算の処理を呼ぶ。

基本項目の非文字列拒否テストを通常の値準備へ置き換える。構造化eventの目的別denyはprocessor接続テストで維持し、level生成とJSON／consoleへの接続はチェーンのテストが担当する。独自型の拒否と上限境界の既存テストは維持する。

今回の検証: 関連テスト310件成功。lint、formatチェック（675ファイル）、旧定義・旧ヘルパー参照確認、git diff --checkが成功した。全体単体テストとmake test-integrationはユーザー指示により保留した。


## 検査件数の加算と計上箇所の統一（2026-09-18）

Problem: 残り件数の減算と、辞書の事前確認・除外コールバック・再帰検査の計上が分散していた。

Evidence: フィールド準備、辞書・配列の検査、processorからの件数引き継ぎ、関連310件のテストを確認した。

Invariants: 検査128件ちょうどまで許可し、129件目は検査せず共有予算超過の固定出力にする。禁止項目・不正キーも一度だけ数え、局所置換・文字数予算・遅延走査・入力非変更を維持する。

Non-goals: 文字数予算の表現、各上限値、サニタイズ規則、実アプリのログ設定は変更しない。

Done: 検査件数の加算を共通の列挙処理へ集約し、関連テストと静的チェックが成功する。

値の準備処理は `visited_entry_count` を0から加算する。`iter_counted_items()` は次の検査が上限を超えないことを確認し、件数を加算してから項目を渡す。フィールドの準備・ネストした辞書・配列はこの列挙処理を使う。独立した予算確認・消費ヘルパーと、再帰検査の計上済み引数は廃止する。deny除外のコールバックは診断記録だけを行う。

トップレベルの入力検査件数は選別完了後にpreparerへ引き継ぎ、選択済みフィールドは `already_counted=True` で二重計上を避ける。ネスト・例外・診断の検査は同じ加算カウンターを継続する。

共通列挙処理の遅延加算・上限直前と超過時の停止・計上済み項目の扱いと、辞書・配列・要素が一度ずつ数えられることを `test_value_conversion.py` で確認する。既存の予算境界・deny除外・例外・診断・rendererのテストは維持する。

今回の検証: 関連テスト314件成功。lint、formatチェック（675ファイル）、旧名参照確認、git diff --checkが成功した。全体単体テストとmake test-integrationはユーザー指示により保留した。


## 検査開始位置で件数管理を明示（2026-09-18）

Problem: 列挙ヘルパーに隠れた件数更新と列挙ごとのindex上限が、共有件数の意味と計上タイミングを読みにくくしていた。

Evidence: フィールド・辞書・配列の計上経路と、関連314件のテストを確認した。

Invariants: 検査128件ちょうどは許可し、129件目の検査前に停止する。トップレベルの計上済み境界、denyや不正キーの計上、局所置換、共有予算超過の固定出力を維持する。

Non-goals: 文字数予算・上限値・サニタイズ規則・実アプリの設定は変更しない。

Done: 各検査ループの先頭で上限確認と加算が読め、関連テストと静的チェックが成功する。

列挙ヘルパーと列挙ごとのindexチェックを廃止する。フィールドの準備・辞書キー検査・配列要素検査で、共有のvisited_entry_countがVALUE_LIMIT以上なら停止し、それ以外は一件加算して検査する。計上済みのトップレベル項目は確認・加算を省略する。再帰先の値検査とdeny除外のコールバックでは加算しない。

列挙ヘルパーの直接テストは辞書の遅延走査・上限停止と、フィールド準備の計上済み動作へ移行した。既存の共有予算と二重計上防止のテストは維持した。


## 文字列検査の集約と合計文字数の加算（2026-09-18）

Problem: 文字列の局所置換と全体文字数の更新が別メソッドに分かれ、残り文字数の減算も処理の流れを読みにくくしていた。

Evidence: 文字列・トップレベルキー・ネストしたキーの処理と、関連314件のテストを確認した。

Invariants: 単一4000文字・合計16000文字ちょうどは許可し、単一超過の原文は合計に加えずその値だけ置換する。共有文字数の超過はログ全体の固定出力に切り替える。deny除外と共有走査件数は維持する。

Non-goals: 制限値・サニタイズ規則・診断出力・実アプリのログ設定は変更しない。

Done: 文字列の個別上限確認・合計への加算・全体上限確認を一箇所で読め、関連テストと静的チェックが成功する。

合計文字数はcounted_text_charsを0から加算する。inspect_textは長さを取得し、単一上限なら置換し、それ以外は合計へ加算して全体上限を確認する。超過を検出した時点で準備を中断する。キーは各検査箇所で長さやdenyの判定を通過した後に同じカウンターへ加算し、全体上限を確認する。文字数計上だけのヘルパーは廃止する。

文字列とキーの合計境界、局所置換で正常な文字列の合計を戻さないこと、上限超過時の固定出力は既存テストで確認する。内部状態の検証は残り文字数から累積文字数へ移行した。


## 辞書一項目の検査を一箇所へ集約（2026-09-18）

Problem: 辞書の件数・キー検査、deny除外、診断記録、値の再帰検査が複数の関数に分散し、処理順を追いにくかった。

Evidence: 辞書の検査・共通deny除外・直接テストと関連314件のテストを確認した。

Invariants: 件数はキー検査前に一度だけ加算し、不正キーと禁止キーの値には触れない。正規化と確定済みdeny、文字数の加算順、局所置換、共有予算超過、入力非変更を維持する。

Non-goals: トップレベルの工程構成、サニタイズ規則、各上限値、診断出力は変更しない。

Done: inspect_mapping内で一項目の処理順を読め、関連テストと静的チェックが成功する。

辞書のループ内に件数確認・加算、キー型、キー長、deny判定と除外記録、文字数加算と全体上限確認、値の再帰検査と格納を並べる。辞書項目の検査用イテレーターと除外コールバックは廃止する。denyの照合式はトップレベルとネストの2箇所に置き、正規化処理と規則集合は共通とする。

旧ヘルパーのテストは辞書検査の計上順と走査停止へ移行し、ネストの直接deny判定にもキーの表記揺れが効くことを追加確認する。


## deny・mask・sanitize の責務分離（2026-09-19）

Problem: denyが構造化項目の除外と文字列内の値のマスクを兼ね、sanitizeの名前にもキー指定による処理が混在していた。

Evidence: 基底・目的別ルール、文字列処理、値準備、processor、例外・診断の経路と契約テストを確認した。

Invariants: 既存の認証情報・記事本文の保護、deny優先とトップレベルのみのallow、構造・上限検査、入力非変更、失敗時の固定出力を維持する。deny・maskはそれぞれ基底と親の規則を継承し、相互に暗黙追加しない。

Non-goals: 辞書の値全体のmask、検出パターン追加、アプリの既存ログチェーン置換、API・DB・依存関係の変更。

Done: 通常値・例外・診断の出力箇所でsanitize_text → mask_assignmentsを直接呼び、責務の独立性と処理順の組み合わせをテストで確認し、バックエンド検証を通す。

検証: ruff lint・format成功。ログ関連411件、バックエンドのDB不要テスト7,330件、`make test-integration` 1,369件が成功した。通常の `pytest tests/` はDB未起動で停止したため、DB不要分とテスト用DBを起動する結合テストに分けて全体を確認した。


## 例外固有の抽出とログの組み立ての整理（2026-09-19）

Problem: 共通の中間型ExceptionCauseにSQL固有のSQLSTATEが含まれ、最終出力のExceptionLogFieldsと定義が重複していた。

Evidence: exceptions/sql.py・validation.py・cause.py、safe_exception_log.py、processorと例外ログの既存テストを確認した。

Invariants: SQL実データと検証入力の除去、SQLSTATEの形式確認と省略条件、例外型・frame、抽出失敗時の固定メッセージ、出力フィールドと後段の値準備を維持する。

Non-goals: 新たな例外種別・診断項目・変換層・出力型の追加、構造検査やサニタイズ・マスク規則の変更。

Done: ExceptionCauseを削除し、SQLから原因文とSQLSTATEを個別に取り出し、validationから説明文を取り出して一つのExceptionLogFieldsへ組み立てる。既存の保護とSQLSTATEの任意性をテストで確認し、バックエンド検証を通す。

検証: ruff lint・format成功。ログ関連417件、バックエンドのDB不要テスト7,336件、`make test-integration` 1,369件が成功した。SQLSTATEの欠落・不正値・pgcode経由の取得・取得失敗時の出力を確認した。


## processorの戻り値と工程の明示（2026-09-19）

Problem: 共有予算超過の固定出力を診断が持ち、processorの処理本体から超過時に何が返るかを読み取れなかった。選別のメソッド名からは診断への記録が読めず、例外の処理は入力走査の前・中・後へ分かれていた。

Evidence: processor・diagnostics・field_selection・safe_exception_logと関連テストを確認した。app配下で `exc_info` と同名項目を同時に渡している箇所は見つからなかった。

Invariants: 共有予算超過時の固定の3項目、処理失敗時の固定エラー、deny優先、未登録除外、不採用の値に触れないこと、同名項目の例外由来優先、入力非変更を維持する。

Non-goals: 予算の計上規則・上限値・サニタイズとマスクの規則・出力フィールド名の変更。

Done: 診断は最後まで検査を終えたログへ付与する記録だけを担当し、共有予算超過の固定出力はprocessorが直接返す。`LogFieldSelector.should_include` を `select` へ改名した。同名入力の検査省略を廃止し、例外の抽出・計上・準備を入力走査の後へまとめた。同名の入力値は検査と予算計上の対象になる。

検証: ruff lint・format成功。ログ関連の単体テスト486件が成功した。integrationはDB・repositoryに触れていないため実行していない。


## 例外frame数と走査件数の上限を実測で調整（2026-09-19）

Problem: 例外frame数の上限10では、DB・通信の例外でframesが常に `[limit]` になり発生位置を残せなかった。

Evidence: asyncpg・SQLAlchemy・httpx・FastAPIで実際に例外を発生させて測った。素の3階層でDB例外は22〜37frame、httpxの接続失敗は13frameだった。ミドルウェアと `Depends` を含む構成では、service・routeで捕まえると41〜42frame、全体の例外ハンドラで捕まえると62frameだった。framesは1frameあたり4件・約112文字を消費する。app配下のログ呼び出し211件のキーワード引数は最大9個だった。

Invariants: 上限を超えたframesは全体を `[limit]` に置換し部分リストを残さない。走査件数の超過はログ全体を固定出力へ置換する。文字数の上限、計上規則、frameに値やソース行を含めないことを維持する。

Non-goals: 全体の例外ハンドラでの `exc_info` 付きログへの対応（現在のappに該当箇所はなく、必要になれば70frame・384件程度を再検討する）。SQLパラメータの走査上限と、短いパラメータ値が原因文を広く置換する挙動の変更。

Done: `FRAME_LIMIT` を50、`MAX_ITEMS_PER_LOG_EVENT` を256にした。framesが上限ちょうどのとき約205件を使い、通常項目に約51件が残る。合計文字数16000は据え置いた。

検証: ruff lint・format成功。ログ関連と測定スクリプトの単体テスト944件が成功した。上限を1件だけ超える境界テストの入力を定数から導く形へ直した。integrationはDB・repositoryに触れていないため実行していない。
