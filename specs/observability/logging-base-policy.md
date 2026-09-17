# アプリケーションログの共通基底ポリシー

作成: 2026-09-17
Status: Partially implemented
Implementation: 基底規則・processor・例外構造化・チェーン構成を `backend/app/log_policy/` に実装済み。本文の目的別 deny を定義済み。目的別 allow と既存 structlog チェーンの置き換えは未実施。

関連: [アプリケーションログの概念別ポリシーとCloudWatch集約](./application-logging-policy.md)、[デプロイ診断ログの共通秘匿ポリシー](../platform/deployment-log-policy.md)

## Problem

structlog の処理チェーンに共通の禁止規則がなく、秘匿は呼び出し側の規律 (`redact_secrets` の手巻き、`logger.exception()` の回避) に依存している。結果として、例外文を丸ごと捨てる箇所と生で出す箇所が混在する。

ログポリシーは「処理の目的」ごとに定義し、同じ目的の処理は bounded context が違っても同じポリシーを使う。本書はその土台として、**目的に依存せず出してはいけないもの**だけを基底として定める。迷うものは基底に入れず、実装中に必要性が見えた時点で追加する。

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
共通基底 (本書)          目的に依存しない deny と除外規則。目的ポリシーは緩められない
  └─ 目的ポリシー (別途)  基底を継承し、自分の allow / mask / deny を足す
       外部コンテンツ取得 / AI 推論 / 利用者対話 / パイプライン制御 / 基盤接続
```

キーに対する指定は次の 4 種で、`deny` は認証情報を基底、本文などの業務データを目的別ポリシーが所有する。`allow` / `mask` も目的別ポリシーが所有する。

| 指定 | 動作 | 記録 |
| --- | --- | --- |
| `deny` | キーごと落とす。allow に入っていても効く | `_denied_keys` にキー名 |
| `allow` | 値を出す。文字列は sanitize を通す | — |
| `mask` | 存在だけ出し値は出さない。出力形と導入は必要とする目的ポリシーの実装時に決める (v1 では実装しない) | — |
| 未登録 | どれにも該当しない。キーごと落とす | `_unregistered_count` に件数 |

- 継承はクラス継承ではなく合成で表す。目的ポリシーの有効な deny は `基底 deny ∪ 目的別定義の deny ∪ 追加 deny` であり、引く操作は存在しない。
- `allow ∩ 有効な deny = ∅` を `LogPolicyRules` 構築時に検証する。目的別定義との合成時にも再検証し、登録時点で違反を拒否する。processor の実行時判定と二重に保証する。
- deny と allow はどちらもキー名の正規化後 (小文字・camelCase / PascalCase / 略語境界 / ハイフン → snake_case) の**完全一致**で判定する。部分一致や正規表現を使うと `completion_tokens` `rds_iam_auth_token_port` 等を巻き込み、例外リストが必要になる。基底に例外 (逃げ道) を置かないための条件である。
- logger は構築時に適用するポリシーを宣言する。宣言のない logger は基底のみで処理する (漏れる方向ではなく出ない方向に倒す)。
- 明示禁止は IAM と同じ `deny` と呼ぶ (explicit deny は allow に常に優先する)。OpenTelemetry Collector の redaction processor は allow-list 非該当の削除を "redacted"、値の置換を "masked" と呼ぶため `redact` は使わない。Pydantic の `extra="forbid"` (未登録拒否に近い別概念) と混同しないよう `forbid` も使わない。
- アプリログは Logfire から切り離す。Logfire は trace / span 専用とし、structlog のチェーンは本ポリシーが独自に構成して stdout JSON → CloudWatch へ出す。
- 階層 (public / restricted) は出力先の軸であり本書では扱わない。アプリの stdout → CloudWatch は restricted 単一とする。

## Invariants

- 共通の認証情報禁止・例外保護・未登録除外は、どの目的の処理からも、bind / contextvars / ログ引数のどこから来た値にも同じく適用される。
- 基底のキー deny は認証情報だけを定める。本文の禁止と allow は目的別に持つ。
- 目的ポリシーは基底の deny を継承して足すことしかできない。基底より緩いポリシーは定義できない。
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

- deny キーの正本は `rules.py` の `CREDENTIAL_KEYS`。`password` / `passwd` / `pgpassword`、`secret` / `private_key` / `client_secret`、`token` / `access_token` / `refresh_token` / `id_token`、`api_key` / `x_api_key` / `x_goog_api_key`、`authorization` / `proxy_authorization`、`cookie` / `set_cookie`、`access_key_id` / `secret_access_key` / `session_token` と各 `aws_` 接頭辞版、`x_amz_signature` / `x_amz_credential` / `x_amz_security_token` を含む。正規化後の完全一致で、値の長さや見た目によらず落とす。
- アプリ固有の設定名も明示 deny に含める: `gemini_api_key` / `openai_api_key` / `deepseek_api_key` / `tavily_api_key` / `logfire_token` / `bff_jwt_signing_secret` / `revalidate_bearer_secret` / `postgres_auth_password` / `postgres_app_password` / `postgres_collect_password`。`app/config.py` と `app/db/settings.py` の認証情報定義を確認済み。
- 値の sanitize: 既知のキー付き値、provider key / JWT / PEM 秘密鍵 / DSN userinfo を置換する。キー付き値は長さを問わず、引用符内の空白・改行・エスケープを値全体として扱う。閉じていない引用符は末尾まで伏せる。引用符なしの一般値は空白等までの単一トークン、認証ヘッダー・cookie は行末までを対象とする。任意の文字列や別名で渡された認証情報の完全検出は保証しない。パターン集は deployment-log-policy の S1/S2 表に対応し、`log_policy` がログ出力用の正本として所有する。監査 DB `error_message` 用の既存 `redact_secrets` (`app/shared/security/`) はこの step では変更せず、監査経路を本ポリシーへ寄せるかは組み込み step で判断する。
- **含めないもの**: ARN、account ID、endpoint、host、SSM parameter path。これらは識別情報であり、基盤接続ポリシーが allow を判断する。DSN は「userinfo を伏せ host を残す」変換になるが、基底が保証するのは userinfo 部分だけである。

### 2. 目的別 deny との境界（記事本文）

`BASE_DENY = CREDENTIAL_KEYS` とし、本文・派生テキストは含めない。`policies/article_text.py` の禁止項目を `policies/external_content.py` と `policies/ai_inference.py` がそれぞれ採用する。目的名との対応は `registry.py` の `POLICIES` が所有する。選択した目的の deny は有効 deny に必ず含め、呼び出しごとに追加指定する必要はない。

基盤接続などでは `description` を明示 allow すれば診断情報として残せる。共通 deny から外した項目も、allow に未登録なら引き続き出力しない。本文を扱う目的を増やす場合は、その目的の deny を明示する。

- deny キー: `body` `content` `text` `html` `description` `summary` `translation` `key_points` `snippet` `answer`
- 上記2目的での対象は取得 HTML、抽出本文、整形本文、翻訳、要点、引用抜粋、AI 生成文。先頭 N 文字や debug レベルを例外にしない。
- 代替は `body_length` `has_body` 等の計量値と記事 ID・URL。
- `title` は v1 では対象にしない。
- dict / list / tuple 内でも禁止キーを再帰的に除外する。選択した目的の有効 deny を文字列・例外文の sanitize にも渡し、`content='...'` 等の既知キー付きの表現を伏せる。本文を禁止しない目的へ本文用 sanitize を暗黙適用しない。自由文として埋め込まれた記事本文の完全検出は保証しない。
- 入力値を漏らさない例外保護は引き続き共通とする。Pydantic `ValidationError` は `str(exc)` を使わず、件数と標準エラー分類だけを抽出する。`input` / `ctx` のほか、入力を含み得る `msg` / `loc` / `title` / カスタム分類文字列も出さない。フィールド別の調査情報は目的ポリシーで別途定義する。

### 3. SQL パラメータ

SQLAlchemy 例外の `str(exc)` に連結される `[SQL: …]` と `[parameters: …]` を除外する。例外型・frame を残し、primary message は保護してから出す。driver の `sqlstate` / `pgcode` が英大文字・数字5文字なら `sqlstate` として別に残す。

- `hide_parameters=True` は維持するが、それで保護済みとは扱わない。
- `args[0]` から `DETAIL` / `HINT` / `CONTEXT` / `QUERY` / `STATEMENT` / `LINE N` 以降を除外する。primary message 中にもパラメータが入り得るため、`params` の組み込み型の値と、その既知のエスケープ表現を置換する。constraint 名もパラメータと一致する部分は伏せる。
- パラメータが循環・深さ超過・件数超過・独自型の場合、message を固定文にする。未知の driver 独自表現に含まれる値の完全検出は保証しない。新しい例外形式は個別の抽出規則と回帰テストを追加する。

### 4. 未登録キー

宣言されたポリシーの allow / mask にも有効な deny にも該当しないキーはキーごと落とす。

- 未登録キーは名前・値とも出さず、`_unregistered_count` に件数だけを記録する。top-level でも kwargs 展開によって入力由来の名前が入り得るため、コード由来と仮定しない。
- deny に該当した場合は `_denied_keys` に記録し、未登録と区別する。前者は bug、後者は宣言漏れ。
- 目的ポリシー導入後、正常系で `_unregistered_count` が出ないことを契約テストで検証する（全体へのCI適用は未実施）。

### 保護処理の境界と失敗時

- `event` / `level` / `timestamp` / `logger` / `logger_name` も共通 deny と値の保護を通す。予約名は保護の迂回手段にならず、非文字列の予約値は固定マーカーにする。
- `stack` / `stack_info` / `exception` / `_record` / `_from_structlog` は renderer へ転送しない。スタック調査には `exc_info` から抽出した frame metadata を使う。
- 出力値は組み込みの JSON 相当型に限定する。bytes / set / 独自オブジェクト / 組み込み型の subclass は固定マーカーにし、`repr` / `__structlog__` 等を呼ばない。
- ネストは深さ10、値はイベント内で1000件、top-level 項目も1000件まで。循環・上限超過・非有限浮動小数・巨大整数は固定マーカーで示す。共有参照は循環扱いしない。
- 例外の `__str__` が失敗しても型と frame を残し、message は固定文にする。processor 自体の失敗は入力を含まない `log_policy_failed` に置き換え、業務側へ例外を伝播させない。原文 fallback や保護処理からの再帰ログはしない。
- ネスト内の未登録キーの allow 制御、自由文中の未知の秘密・本文の判別は別の保証であり、共通 deny の完全一致検査だけでは保証しない。

## 適用位置

- API / worker / Lambda 共通の structlog 構成を本パッケージが持ち、processor を renderer の直前に置く。既存の `setup_logfire` (`logfire.StructlogProcessor` を含む) と `setup_lambda_logging` は、組み込み時にこの構成へ置き換える。
- 順序: deny 除外 → 未登録除外 → 残った文字列値の sanitize → 切り詰め。落とすキーの値は sanitize しない。切り詰めを sanitize の前に行わない (断片が残る)。
- 例外は `format_exc_info` の代わりに基底が構造化する。processor が `exc_info` を消費し、型 FQN、規則 3 を通した message、frame metadata (file / function / line。locals・ソース行は含めない) に置き換える。renderer には `exc_info` が届かないため、dev console も traceback 文字列を出さない。cause chain の扱いは目的ポリシー実装時に決める。
- 配置は `backend/app/log_policy/`。既存チェーンの置き換えは、目的ポリシーの定義と `policy_logger` への移行を終えてから行う (置き換え時点で未宣言 logger の全フィールドが落ちるため)。

## Non-goals

- 目的ポリシーの allow 一覧、module との対応表 (別文書)。
- 利用者テキスト (question / previous turn) の禁止。利用者対話ポリシー側で定める。
- 任意の自由文の意味解析、未知の例外形式からの入力値の完全抽出、ExceptionGroup の子例外展開。traceback の locals は常に除外する。
- public / restricted の profile 引数、Logfire の trace / span 側の変更、frontend、監査 DB `error_message` の契約変更。
- 既存ログの再加工、`title` の扱い、`mask` の出力形。

## Verification

合成値のみを使い、実 credential・実ログを使わない。

| ID | 条件 | 期待結果 |
| --- | --- | --- |
| B01 | 禁止キーを bind / contextvars / 引数から渡す | 3 経路すべてでキーが出ず `_denied_keys` に名前だけ残る。 |
| B02 | 許可値の文中に AWS secret key / DSN userinfo / Bearer token | 値全体が置換され、他の文は残る。 |
| B03 | AWS ARN / RDS endpoint を許可キーで渡す | 基底では置換しない (識別情報は目的ポリシーの判断)。 |
| B04 | 仮の deny を定義し、ネスト dict・list・既知キー付き例外文を渡す | 指定した禁止値を除外し、許可した計量値は残る。実際のAI・記事取得の禁止項目の妥当性は別途検証する。 |
| B05 | SQL / parameters 連結、DETAIL、primary message 内の bind 値 | 対象値を除外し、型・SQLSTATE・保護後の primary message・frame が残る。 |
| B06 | ポリシー未宣言 logger に任意キー | 業務キー除外、`_unregistered_count` に件数のみ。 |
| B06a | 目的ポリシーの allow に基底 deny のキーを含めて定義 | 定義時に失敗し、processor に到達しない。 |
| B06b | allow に `completion_tokens`、deny に `token` | 完全一致なので `completion_tokens` は出る。 |
| B07 | 上限超の長文末尾に secret | 切り詰め後に断片が残らない。 |
| B08 | production JSON と dev console | 秘匿結果が一致し、整形で原文が復活しない。 |
| B09 | 予約キー、独自オブジェクト、循環・深い・大量の値、処理失敗 | 迂回や原文 fallback がなく、保護処理で業務を落とさない。 |

所在テスト ([backend/tests/log_policy/](../../backend/tests/log_policy/)):

- B01 / B03 / B04 / B06 / 文字列によるポリシー指定の拒否 / event の sanitize: `test_processor.py` (実チェーン + `LogCapture` で検証。`capture_logs` は configured processors を差し替えるため使わない)
- B02 / 通常テキストの保持: `test_sanitize.py`
- B07 / 通常値・例外文・型名・frameの出力上限: `test_output_limits.py`
- B05 / frame metadata / `exc_info` の 3 形式: `test_safe_exception_log.py`
- B06a / B06b / キー正規化: `test_rules.py`
- 共通認証情報の全目的への適用、仮の追加 deny の文字列・例外文への適用: `test_policy_boundaries.py`
- B01 / B02 / B04 / B05 / B09 の回帰条件: `test_base_guards.py`
- B08: `test_chain.py` (`JSONRenderer` と `ConsoleRenderer` を `build_processors` で通す)

共通deny強化時の検証 (2026-09-17、責務分離前):

- `uv run ruff check app/ tests/log_policy` / `uv run ruff format --check app/ tests/log_policy`: 成功。
- `uv run pytest tests/ -m unit -x -q`: 7017 passed、1369 deselected。DB テストは下記の専用環境で実行する。
- 追加の予約値・末尾エスケープ回帰条件を含む最終版: `uv run pytest tests/log_policy --confcutdir=tests/log_policy -q`: 102 passed。
- `make test-integration`（専用 project、Compose は `--env-file /dev/null`）: 1369 passed。テスト用 DB / Redis と network の削除まで完了。
- 全体テストは作業用の起動スクリプトでルート `.env` の読込を無効にし、合成設定のみを注入した。製品の設定・テスト fixture は変更していない。

既存チェーンは未置換のため、実プロセスの stdout での確認は組み込み step で行う。

## Implementation

- [backend/app/log_policy/rules.py](../../backend/app/log_policy/rules.py): `LogPolicy`、`BASE_DENY`、`normalize_key`、`LogPolicyRules` (定義時検証)
- [backend/app/log_policy/sanitize.py](../../backend/app/log_policy/sanitize.py): S1/S2 パターン集、`sanitize_text`（禁止値の置換のみ。長さ制限は出力側）
- [backend/app/log_policy/exception_messages.py](../../backend/app/log_policy/exception_messages.py): 例外種別の分岐、SQL実データの除外、入力検証の件数・分類抽出、SQLSTATE抽出
- [backend/app/log_policy/safe_exception_log.py](../../backend/app/log_policy/safe_exception_log.py): 抽出 → 共通文字列保護 → 500文字制限 → 型・原因文・SQLSTATE・frameの組み立て
- [backend/app/log_policy/policies/ai_inference.py](../../backend/app/log_policy/policies/ai_inference.py)、[external_content.py](../../backend/app/log_policy/policies/external_content.py): 目的別定義（allow は未定義のため空）
- [backend/app/log_policy/registry.py](../../backend/app/log_policy/registry.py): 名前と目的別定義の対応、登録時の合成・検証、識別子の解決
- [backend/app/log_policy/selection.py](../../backend/app/log_policy/selection.py): フィールドの採否、予約・内部フィールドの扱い、除外診断
- [backend/app/log_policy/value_protection.py](../../backend/app/log_policy/value_protection.py): 採用値の秘匿・型変換・ネスト deny・循環とサイズ制限
- [backend/app/log_policy/processor.py](../../backend/app/log_policy/processor.py): 解決 → 選別 → 値保護 → 例外抽出の接続と固定の失敗イベント
- [backend/app/log_policy/logger.py](../../backend/app/log_policy/logger.py): `policy_logger`
- [backend/app/log_policy/chain.py](../../backend/app/log_policy/chain.py): `build_processors` (既存チェーンへの組み込みは未実施)

## Done

- [x] 共通の認証情報 deny と目的別の本文 deny が区別され、processor で合成して適用できる。
- [ ] API / worker / Lambda のチェーンを `build_processors` へ置き換える (目的ポリシーと `policy_logger` 移行の完了後)。
- [x] B01〜B09 の上記限定された保証範囲が合成値で通る。
- [ ] Evidence の生 `str(exc)` 箇所が基底を通した出力になっている。
- [x] 目的ポリシーの追加が、基底の変更なしに allow / deny の追加だけで可能である (`mask` は未実装)。

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

単独の境界検証は `test_components.py`、チェーンを通す既存の出力検証は従来のテストが所有する。

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

Problem: 例外の型別抽出と文字列保護・出力組み立てが混在し、対象と保護規則の対応を追いにくい。
Evidence: `sanitize.py` / `safe_exception_log.py` / `value_protection.py` の全呼び出しと既存の契約テストを確認した。
Invariants: 既存の禁止値除外、SQL・入力検証例外の抽出結果、失敗時の固定文、型名・frame・通常値の長さ上限を維持する。秘匿してから切り詰める。
Non-goals: AIポリシーの定義、禁止項目・抽出内容の変更、既存アプリのログ設定への接続。
Done: 型別の抽出先と適用順序を明示し、最終出力の回帰テストとバックエンド検証を通す。

- `exception_messages.extract_exception_message`: SQL例外 → `extract_sql_error_message`、入力検証例外 → `extract_validation_error_summary`、その他 → 通常の原因文。共通の認証情報置換や文字数制限は行わない。
- `sanitize.sanitize_text`: 共通認証情報と指定された禁止キー付き値を置換する。文字数制限は行わない。
- `safe_exception_log.build_exception_log_fields`: 対象固有の抽出後、共通文字列保護、500文字制限、フィールド組み立てを行う。SQLSTATEの取得失敗時の固定文も維持する。
- `ExceptionLogFrame`: 引き続き `file` / `function` / `line` の出力形。`ExceptionLogFields`: `SafeExceptionLogFields`から改名した出力形。型そのものが安全性を検証するという保証は持たせず、生成処理が保護を担う。
- `ValueProtector`: 通常値とネストしたキー名を秘匿してから500文字に制限する。

文字数制限の既存テストは `test_output_limits.py` へ移し、例外文・型名・frameについても出力側の秘匿順序と上限を検証する。`test_sanitize.py` は置換のみの契約を検証する。

今回の検証:

- lint / format / `git diff --check`: 成功。
- 対象テスト: 120 passed。バックエンド単体テスト: 7039 passed、1369 deselected。
- `make test-integration`: 初回はRedisの `begin_attempt` が `None` を返すケースで1件失敗（4 passed）。元の例外が記録されていないため原因は未確定。コード・テスト・設定を変更せず同条件で再実行し、1369 passed。専用DB / Redisコンテナとnetworkの削除まで完了。
- 全体テストは `.env` を読まない検証用起動スクリプトと合成設定で実行した。Composeには専用projectと `--env-file /dev/null` を指定した。
