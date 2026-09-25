# ログの情報漏洩防止と項目別サニタイズの責務分離

作成: 2026-09-24
Status: Implemented
Implementation: 情報漏洩防止を [leak_prevention.py](../../backend/app/log_policy/leak_prevention.py)、項目別サニタイズの対応表と `sanitize_article_url` を [sanitize.py](../../backend/app/log_policy/sanitize.py) に実装済み。実際のポリシーでの `sanitize` の有効化は後続とする。

[共通基底ポリシー](./logging-base-policy.md)と[項目別サニタイズのログポリシー](./logging-sanitization-policy.md)の後続として、全文字列に適用している処理を情報漏洩防止として定義し直し、項目別サニタイズとの責務を分ける差分仕様。文字列内部の処理とサニタイズの対象は本書を優先する。

## Problem

全文字列に適用している `sanitize_text` と `mask_assignments` は、どちらも「項目によらず文字列に紛れた認証情報を伏せる」処理だが、サニタイズ・マスクという別概念の名前で呼ばれ、`mask_assignments` はポリシーの `mask` 集合を受け取っている。項目別サニタイズの対応表にも同じ検出処理が登録され、サニタイズと漏洩防止の違いがコードとテストから読み取れない。

## Evidence

- [sanitize.py](../../backend/app/log_policy/sanitize.py) (変更前): 形式ごとの検出と `sanitize_text`、項目別サニタイズの対応表 (`connection_url → sanitize_url_userinfo`、`upstream_message → sanitize_jwts`) を同じモジュールに持つ。AIプロバイダーのキーは `_PROVIDER_KEY_PATTERNS` にまとめて検出し、アプリが保持しない GitHub・Anthropic・Tavily・OpenAI の形式を含む。DeepSeek の形式はない。
- `mask.py` (Step 3 で削除): 文字列中のキー付き値の終端を引用符・括弧・区切り文字から推測する。値に区切り文字が含まれると残りが出力される (`password=abc,def` → `password=***,def`)。
- [value_preparation.py](../../backend/app/log_policy/value_preparation.py)・[diagnostics.py](../../backend/app/log_policy/diagnostics.py): 文字列値・辞書キー・除外キーの診断へ `sanitize_text → mask_assignments(mask=rules.mask)` を適用する。
- [config.py](../../backend/app/config.py) と Lambda の設定: アプリが保持する形式を持つ認証情報は Gemini・DeepSeek・Logfire。`openai_api_key` は定義のみで読まれず、Tavily は実行経路で生成されない。
- Gemini SDK はキー漏洩時に `API key AIza... has been reported as leaked` を返し、原因連鎖の `error_message` として出力される。キー名の目印がなく、形式による検出だけが保護になる。
- 漏洩防止の単体・適用箇所・出力後のテストが15ファイルに分散し、URL userinfo・ARN/RDS・適用順・レンダリング後の保護を重複して検証している。
- OWASP Logging Cheat Sheet・ASVS 16・NIST SP 800-53 AU-3(3) は出力の最小化を主な統制とし、OpenTelemetry Collector・Logfire・CloudWatch・Datadog・gitleaks は許可の内側で既知の種類を列挙して検出する。Google Sensitive Data Protection・Presidio・Logfire は種類を示す置換表記を既定とする。

## 保護規則の役割

| 規則 | 対象 | 判断の基準 | 出力 |
| --- | --- | --- | --- |
| `deny` | 項目ごと出さないもの | 項目名 | 項目を削除する |
| `mask` | 項目の存在は残すが値は不要なもの | 項目名 | 値全体を `***` にする |
| `sanitize` | 調査のために残したいが、そのままでは出せないもの | 項目名とその項目の意味 | 調査に必要な部分だけを残した値 |
| 情報漏洩防止 | 絶対に出してはいけない認証情報 | 文字列の内容 | 該当部分を `[redacted:<種類>]` にする |

`deny`・`mask`・`sanitize` はポリシーで項目を指定する。情報漏洩防止はポリシーで指定せず、出力するすべての文字列に適用する補助の保護であり、主な保護は `allow`・`deny`・`mask` が担う。

サニタイズは「その項目で調査に要る部分」を定義し、それ以外を落とす。認証情報の検出はサニタイズの責務ではなく、漏洩防止と同じ検出処理を対応表に登録しない。サニタイズの結果が認証情報を含まない形になっていれば、後段の漏洩防止を通しても結果は変わらない。

## 処理順

1. トップレベルの項目の選別 (`allow`・`deny`・未登録)
2. `mask`: 対象項目の値全体を `***` にし、その値の処理を終える
3. 構造・予算検査とネストの `deny`
4. `sanitize`: 登録された項目の値だけを処理する
5. 情報漏洩防止: 文字列値・ネストの辞書キー・除外キーの診断へ適用する
6. renderer

固定マーカー (`***`・`[limit]`・`[unsupported]` など) は4・5の対象にしない。トップレベルの項目名は置換しない。

## 情報漏洩防止

`backend/app/log_policy/leak_prevention.py` が所有する。入口 `prevent_credential_leaks(text)` が下表の順に種類ごとの関数を呼び、合成と順序を所有する。値準備と診断は入口だけを呼ぶ。ポリシーの `mask` 集合は受け取らない。

| 種類 | 関数 | 検出条件 | 置換 |
| --- | --- | --- | --- |
| `private_key` | `redact_private_key_blocks` | `-----BEGIN [種別 ]PRIVATE KEY-----` から対応する END 行まで。END 行がなければ末尾まで | ブロック全体 |
| `gemini_api_key` | `redact_gemini_api_keys` | `AIza` と英数字・`_`・`-` の35文字 | 一致部分 |
| `deepseek_api_key` | `redact_deepseek_api_keys` | 前後が英数字でない `sk-` と16進小文字32文字 | 一致部分 |
| `logfire_token` | `redact_logfire_tokens` | `pylf_v1_`・英小文字2文字・`_`・英数字1文字以上 | 一致部分 |
| `aws_access_key_id` | `redact_aws_access_key_ids` | 前後が英大文字・数字でない `AKIA` / `ASIA` と英大文字・数字16文字 | 一致部分 |
| `url_userinfo` | `redact_url_userinfo` | 英字を含む scheme に続く `://` から `@` までの userinfo | userinfo だけを置換し、scheme と接続先を残す |
| `jwt` | `redact_jwts` | 先頭2区画が `eyJ` で始まり後続文字を持つ3区画の base64url | `eyJ` の位置から3区画 |
| `credential` | `redact_credential_assignments` | 正規化後に `CREDENTIAL_KEYS` と完全一致するキー名と、閉じ引用符・`=` または `:`。AWS署名付きクエリの認証パラメーター (`X-Amz-Signature` / `X-Amz-Credential` / `X-Amz-Security-Token`) もキー名として扱う | キー名と区切りを残し、その後ろを末尾まで置換する |

- 置換表記は常に `[redacted:<種類>]` とし、種類名は上表の値を使う。固定の接頭辞や元の値の一部は残さない。
- キー名を目印にする検出は、値の終端を推測せず、区切りの後ろを文字列の末尾まで置き換える。後ろが空なら変更しない。最初の一致で残りをすべて覆う。
- 形式による検出は一致した部分だけを置き換え、周囲の文を残す。
- 未知の形式、キー名も構造も伴わないパスワードは検出できない。発生元で `SecretStr` を使い、例外文へ入れないことで防ぐ。
- 記事本文・ユーザー入力は認証情報ではないため対象外とする。項目単位の `deny`・`mask` で扱い、呼び出し側で文字列へ埋め込まない。文字列中の `body=...` などを本文として伏せる処理は廃止する。
- アプリが保持しない GitHub・Anthropic・Tavily・OpenAI の形式は検出対象から外す。設定項目と `CREDENTIAL_KEYS` は変更しない。
- 置換表記は元の値より長くなる場合がある。文字数の上限は置換前に検査し、超過した文字列は切り詰めずに `[limit]` に置き換えるため、断片は残らない。表記が固定長のため、出力の伸びは入力上限の定数倍に収まる。
- AWS署名付きクエリの認証値は `&` を値の終端と推測せず、キー名による検出で末尾まで置き換える。後続のパラメーターを調査用に残す必要が出た場合は、その項目のサニタイズとして扱う。
- ポリシーの `mask` は項目単位だけで働き、文字列の内容には影響しない。基底の認証キーは `deny` が項目ごと除外するため、項目単位の `mask` には入れない。`BASE_MASK` に認証キーを入れていた理由は廃止する文字列内のキー付き値の置換だったため、空にする。

### 名前

情報漏洩防止の関数と置換表記には `redact` を使う。[基底ポリシー](./logging-base-policy.md)では OpenTelemetry Collector が許可外項目の削除を redacted と呼ぶことから `redact` を避けていたが、本ポリシーの削除は `deny`・未登録と呼び、`redact` を別の概念に使っていない。アプリ内の [logfire/redaction.py](../../backend/app/logfire/redaction.py) と [shared/security/redaction.py](../../backend/app/shared/security/redaction.py) も値の置換を redact と呼んでおり、同じ概念に同じ語を使う。

## 項目別サニタイズ

`sanitize.py` は項目名から処理を選ぶ対応表と共通入口 `sanitize_field_value(field_name, value)` だけを持つ。

- 対応表には、調査に要る部分を定義した処理だけを登録する。情報漏洩防止の検出処理は登録しない。
- 最初の登録は `canonical_url`・`source_url` → `sanitize_article_url` とする。実際のポリシーでの有効化は後続とし、基底の `sanitize` は空のままとする。
- ポリシーの `sanitize` に対応表にない項目名があっても、構築時には拒否しない。その項目の値は `[unsupported]` にし、ログの他の項目はそのまま出力する。
- 共通入口は正規化済みの項目名を受け取り、対応表にない項目名、または `str` でない値には `[unsupported]` を返す。

### `sanitize_article_url`

記事 URL を調査に使える形に絞る。どの記事・どのサイト・どのページかを特定し、同じ URL で再取得できることを目的とする。

| 部分 | 扱い |
| --- | --- |
| scheme・host・port・path・クエリ | 残す |
| userinfo | 落とす |
| フラグメント | 落とす |

- 追跡用パラメーターの除去は、値の発生元である `CanonicalArticleUrl` の正規化に任せる。log_policy から collection の処理を参照しない。
- userinfo は、URL の分解で除かれるタブ・改行を挟んでいても残さない。
- userinfo もフラグメントもない値は、URL の表記の揃え (scheme の小文字化、タブ・改行の除去など) を除き変更しない。URL として分解できない値は `[unsupported]` にする。
- 例: `https://user:pass@example.com/a/1?p=123#top` → `https://example.com/a/1?p=123`

## テストの配置

| 対象 | 置き場所 |
| --- | --- |
| 漏洩防止の種類ごとの検出境界 | `tests/log_policy/leak_prevention/test_<種類>.py` (`test_private_key`・`test_gemini`・`test_deepseek`・`test_logfire`・`test_aws`・`test_url`・`test_jwt`・`test_assignments`) |
| 漏洩防止の入口 (全種類の適用・通常文の保持・切り詰めない) | `tests/log_policy/leak_prevention/test_prevent_credential_leaks.py` |
| 項目別サニタイズの共通入口と `sanitize_article_url` | `tests/log_policy/test_sanitize.py` |
| 漏洩防止の適用箇所 (値・ネストのキー・例外・診断・サニタイズ後の値) | `test_processor.py` の `TestLeakPrevention`。上限・一度だけの処理・失敗時は各モジュールのテストに残す |
| renderer 後に置換結果が戻らないこと | `test_chain.py` |

`test_sanitize_other.py`・`test_text_preparation.py`・`test_mask_rendering.py` は上記へ統合して削除する。サニタイズの仕組みのテストは `sanitize_article_url` の実際の変換で検証し、テスト専用の処理を登録しない。processor 側のサニタイズ・マスクのテストは、その規則だけで結果が決まる入力を使い、漏洩防止の結果を期待値に含めない。入力と期待値は各テストに直接書く。

## Invariants

- 処理順は「処理順」の1〜6に従い、固定マーカーを後段の処理へ渡さない。
- 共通の型・深さ・文字数・件数・循環参照・非有限数の検査、除外値を検査しない振る舞い、配列の順序を維持する。
- 内容の書き換えは `mask`・`sanitize`・情報漏洩防止に限定し、一律の空白除去や改行削除を追加しない。出力形式のエスケープは renderer が担当する。
- bind・contextvars・ログ引数・抽出例外に同じ完成済みの規則を使い、ログ入力から規則を変更できない。
- 処理失敗時に原文を出さない。業務結果・例外伝播・再試行を変更しない。

## Non-goals

- `allow`・`deny` の意味、例外情報の抽出契約、業務処理の変更。
- 実際のポリシーでの `sanitize` の有効化、未接続ロガーの移行。
- 未知の形式やエントロピーによる汎用検出、文字列中の記事本文・ユーザー入力の検出。
- 設定項目・`CREDENTIAL_KEYS` の整理、SQL例外のパラメーター置換 ([exceptions/sql.py](../../backend/app/log_policy/exceptions/sql.py)) の変更。
- 監査DB・Logfire・デプロイログの秘匿処理の変更、AWSへの適用、新規依存の導入。

## 実装ステップ

| Step | 成果 | 主な検証 |
| --- | --- | --- |
| 1 | 本書と関連仕様の注記。 | 役割・処理順・対象・表記が一意に読める。 |
| 2 | テストを新しい契約へ移し、失敗を確認する。 | 種類ごとの境界、キー名の末尾までの置換、通常文の保持、`sanitize_article_url`、未登録項目名の拒否、適用箇所と renderer 後の保護。 |
| 3 | `leak_prevention.py` を作り、`sanitize.py` の検出処理と `mask.py` を移して削除する。値準備・診断を入口へ切り替え、`BASE_MASK` を空にし、ベンチマークスクリプトの参照を更新する。 | 単体・適用箇所・出力後のテストと、ベンチマークの旧正規表現との一致確認。 |
| 4 | `sanitize.py` の対応表を `sanitize_article_url` に置き換え、構築時の検証を追加する。 | サニタイズと規則構築のテスト。 |
| 5 | 関連仕様の記述を同期する。 | `/check` で変更範囲を検証する。 |

## Verification

2026-09-24 に次を確認した。

- `uv run pytest tests/log_policy -q -m unit`: 606件成功。
  - 種類ごとの検出境界: `leak_prevention/`
  - 共通入口と `sanitize_article_url`: `test_sanitize.py`
  - 未登録項目名の拒否: `test_base.py`
  - 適用箇所: `test_processor.py`
  - 最終出力: `test_chain.py`・`test_logging.py`
- ベンチマークスクリプトの `_check_equivalence`: URL userinfo 20,400件と JWT 30,000件で、置換結果が旧正規表現と一致した。
- `uv run ruff check`・`uv run ruff format --check`: `app/`・ベンチマークスクリプト・`tests/log_policy/` で成功した。
- 実際のポリシーでの `sanitize` の有効化と AWS 上の出力確認は、Non-goals のため実施していない。

2026-09-25 に構築時の検証をやめ、対応表にない項目名の値を `[unsupported]` にした。設定ミスのときにログを残すための逃げ道なので、専用のテストは置かない。

## Done

- 情報漏洩防止の対象の種類・関数・表記が `leak_prevention.py` から読み取れ、種類ごとの単体テストがある。
- 値準備と診断は情報漏洩防止の入口だけを呼び、ポリシーの `mask` は文字列の内容に影響しない。
- サニタイズの対応表は `sanitize_article_url` だけを持ち、対応表にない項目名の値は `[unsupported]` として出力される。
- 最終出力に認証情報が残らず、調査に必要な文と識別情報が残ることをテストで確認できている。
- 関連仕様と実装状態を同期し、未実行の検証があれば理由を明記している。
