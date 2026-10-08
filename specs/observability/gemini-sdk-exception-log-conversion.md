# Gemini SDK例外のログ変換

作成: 2026-10-02 / 更新: 2026-10-08（決定事項の反映）

Status: Accepted
Implementation: 実装済み。

[例外の変換と値の検査の責任分担](./logging-exception-conversion-policy.md)に、Gemini SDK例外の専用の変換を加える差分仕様。SDK例外の原因文と診断項目は、[AI分析のログポリシー](./ai-analysis-logging-policy.md)より本書を優先する。公式例を除く応答内容は合成データである。

## 作業の定義

- Problem: ログ方針を通るロガーに渡した例外の原因チェーンで、Gemini SDK例外のノードが汎用の変換（`str(exc)`）を通り、応答JSON全体（説明文・details）を出している。
- Evidence: SDK、公式API資料、Googleのエラー詳細schema、既存の例外変換（SQL・Pydantic・イベント検証）、Geminiの変換器。
- Invariants:
  - 応答の説明文と、要求の内容（プロンプト・記事本文）を出さない。
  - 失敗の違い（HTTPのstatus、Geminiのstatus、ErrorInfo、枠の種類）を残す。
  - 応答にない値を補わず、知らない値を既知の分類へ丸めない。
  - 変換器の分類・再試行・監査・通知を変えない。
- Non-goals:
  - ログ方針にまだ移行していない工程（curation・embedding・agent・worker）の移行。ECSの全体ログとLogfireの扱い。
  - 変換器の日あたりの判定の削除（後続の作業）。
  - `BadRequest`のfield、HTTPの例外の専用変換。
- Done: SDK例外のノードが本書の形で出力され、下記のテストが通る。

## 現状

- 変換器（[translate_gemini_error](../../backend/app/ai_providers/gemini/error_translator.py)）がSDK例外を4クラスのAIの例外に分類し、`raise translated from exc`で元のSDK例外を原因につなぐ。原因チェーンは「AIの例外 → SDK例外 →（あれば）httpxの例外」になる。
- AIの例外は固定の文と`code`・`reason`で出るが、SDK例外は汎用の変換で`str(exc)`になる。
- ログ方針への移行は途中で、Geminiの例外をログ方針のロガーに渡しているのは今はassessmentのhandlerだけ（`exc_info=completion.error`）。本書の変換は共通の入口に入れるので、ほかの工程も移行した時点で効く。

## SDKの事実

- `APIError`は`details`に応答JSON全体を持ち、`str(exc)`（`"{code} {status}. {details}"`）にも組み込む。`message`と`status`は型を検証せずに取る。非JSONのHTTPエラーでは`message`が本文全体、`status`がHTTPのreason phraseになる。
- HTTP 200で始まったストリームの中のエラーでは、イベント本文の`error.code`から例外を作る（`exc.code=429`・`exc.response.status_code=200`）。agentのストリーミング呼び出しで起こりうる。
- `UnknownApiResponseError`（JSON解析の失敗）は`APIError`を継承せず、説明文に生の応答を含む。原因の`JSONDecodeError`の`str()`は解析の理由と位置だけである。
- `details`の各要素は`@type`で種類が決まる（`ErrorInfo`・`QuotaFailure`・`RetryInfo`・`BadRequest`・`Help`など。[error_details.proto](https://github.com/googleapis/googleapis/blob/master/google/rpc/error_details.proto)）。429では`QuotaFailure`・`Help`・`RetryInfo`が順不同で入る。

## 変換

### 対象と入口

- `google.genai.errors.APIError`（子クラスを含む）と`UnknownApiResponseError`。
- [convert_exception](../../backend/app/log_policy/exceptions/conversion.py)の振り分けに入れ、ログ方針を通るすべてのロガーで効かせる。
- 非AIプロセス（schedulerや週次トレンドのworker）もログ方針を読み込み、起動時に`google.genai`を読み込まない約束がある（`tests/test_lazy_ai_sdk_import.py`）。そのため`google.genai.errors`が読み込み済みかで判定し、変換はSDKの例外が来たときだけ読み込む。

### 原因の文

- `APIError`: `Gemini API error: {provider_code}`。ErrorInfoのreasonがあれば`Gemini API error: {provider_code} / {reason}`。`provider_code`がなければ`Gemini API error`。
- `UnknownApiResponseError`: `Gemini API response could not be parsed as JSON`。原因の`JSONDecodeError`は通常どおり変換する。
- 応答の説明文（`message`）は使わない。入力長の超過だけは、説明文が公開されている形（`The input token count (N) exceeds the maximum number of tokens allowed (M).`）に一致したときに、2つの数値を診断項目へ取り出す。

### 診断項目（`GeminiErrorDetails`）

| 項目 | 取得元 | 検査 |
| --- | --- | --- |
| `kind` | 固定の`gemini` | ― |
| `http_status` | `exc.response`のstatus | 100〜599の整数。boolは拒否。responseがなければ省略 |
| `provider_code` | `exc.status` | 識別子の形 |
| `error_info.reason` / `error_info.domain` | `ErrorInfo`（AIP-193で各型は最大1件） | 識別子の形 |
| `quota_violations[].quota_id` / `.quota_value` | `QuotaFailure`の`violations` | `quota_id`は識別子の形。`quota_value`は数字の文字列（ProtoJSONのint64）を非負の整数にする |
| `retry_delay_seconds` | `RetryInfo`の`retryDelay`（ProtoJSONのDuration。`"53s"`・`"0.500s"`） | 有限・非負の数値 |
| `input_tokens` / `max_input_tokens` | 入力長の超過の説明文 | 上の形に一致したときだけ。非負の整数 |

- 識別子の形: 128文字以内の英数字と`_` `.` `:` `-`。[AI分析ログ仕様](./ai-analysis-logging-policy.md)の`provider_code`の規則と同じ。値の一覧では絞らず、知らない値でも形が合えば残す。
- 形や型が合わない値は、その項目だけ省く。`str()`で直さない。
- 数字は`http_status`だけにし、本文の数字（`exc.code`）で補わない。ストリームの中のエラーは`http_status: 200`と`provider_code`で表す。
- `kind`のほかに取れる項目がなければ、診断項目は出さない（`UnknownApiResponseError`など）。
- `details`は、SDKが保持する`{"error": {...}}`の形で読む（1要素のリストはSDKが展開済み）。知らない形は捨て、自由文や全体の出力に戻さない。
- 違反の件数は絞らない。公開されている実例では最大4件で、外側のAIの例外と合わせて20項目ほど（上限は32）。超えた場合は共通処理の`[limit]`に任せる。

### 出さないもの

- 説明文（`message`）、各詳細の`description`、`Help`のリンク、`LocalizedMessage`、ErrorInfoの`metadata`。
- `quotaMetric`・`quotaDimensions`。quotaIdとログの`model`で枠を特定できる。`quotaDimensions`はキーが動的である。
- `BadRequest`のfield。Geminiが付けるか確認できていない。本番で付いているのが見えたら足す。
- `details`全体、非JSON応答の本文、SDKの属性の再帰コピー。

### 責任分担

| 担当 | 責任 |
| --- | --- |
| detailsの読み取り（`app/ai_providers/gemini/`に1つ） | `details`の形を読み、ErrorInfo・QuotaFailure・RetryInfoを取り出す。変換器とログの変換が共有する。 |
| 変換器 | 失敗の分類。分類は今までどおりの値で行い、結果を変えない。 |
| ログの変換（`app/log_policy/exceptions/`） | SDK例外のノードを固定の文と診断項目にする。識別子の形の検査はここで行う。 |
| AIの例外 | `code`・`reason`だけを持つ。Gemini固有の項目は持たない。 |

#534の観測ログ（`gemini_resource_exhausted`）のquotaIdの形も、識別子の形に揃える。

## 変換後の例

```json
{"error_class": "google.genai.errors.ClientError",
 "error_message": "Gemini API error: INVALID_ARGUMENT / API_KEY_INVALID",
 "error_details": {"kind": "gemini", "http_status": 400,
                   "provider_code": "INVALID_ARGUMENT",
                   "error_info": {"reason": "API_KEY_INVALID", "domain": "googleapis.com"}}}
```

```json
{"error_class": "google.genai.errors.ClientError",
 "error_message": "Gemini API error: RESOURCE_EXHAUSTED",
 "error_details": {"kind": "gemini", "http_status": 200,
                   "provider_code": "RESOURCE_EXHAUSTED",
                   "quota_violations": [{"quota_id": "GenerateContentPaidTierInputTokensPerModelPerMinute",
                                         "quota_value": 1000000}],
                   "retry_delay_seconds": 53}}
```

```json
{"error_class": "google.genai.errors.ClientError",
 "error_message": "Gemini API error: INVALID_ARGUMENT",
 "error_details": {"kind": "gemini", "http_status": 400,
                   "provider_code": "INVALID_ARGUMENT",
                   "input_tokens": 1250000, "max_input_tokens": 1048576}}
```

## テスト

- 変換の単体に加え、既存のprocessorとJSON rendererを通す契約テストにする。
- ケースは、その形が変換まで届く根拠で選び、根拠ごとに確かめ方を変える。

  | 根拠 | 確かめ方 |
  | --- | --- |
  | 公式仕様（error_details.proto・AIP-193・ProtoJSON） | 出さない項目が出ないこと、残す項目が残ることを、変換結果の丸ごと比較で確かめる。 |
  | 実際の応答例 | 例を合成データに置き換え、同じく丸ごと比較で確かめる。 |
  | 使っているSDKの経路 | SDKの契約テストで、SDKが実際に作った例外を変換する。 |
  | 想像した異常な入力 | 漏らさないことと、変換が失敗の固定文に倒れないことだけを確かめる。診断の復元は求めない。 |

- 説明文・`description`・`metadata`・非JSONの本文に置いた合成マーカーが、直接の例外・原因チェーンのどこからも出ないこと。
- 診断の違い（キー不正と一般の引数不正、分と日の枠、HTTP 200の中の429）が残ること。
- 変換器の分類の結果が変わらないこと（既存のテスト）。

## 判断の理由

- 原因の文に説明文を使わない: 説明文はGoogleの自由文で、何が入るかの約束がなく、要求の内容を引用する例がある（`unexpected character: '\x00'`）。`APIError`は要求を持たないので、SQLのように要求の値を引き算することもできない。失うのは主に引数の不正の説明文である。記事によらないものはデプロイの時刻から、記事によるものはDBの記事から調べる。入力長の数値は取り出して残す。
- 一覧で絞らず形で検査する: Googleには分類の一覧がなく、手で持つと知らない障害のときに情報が消える。reason・domain・quotaIdは仕様上の識別子なので、形の検査で自由文をはじける。
- ログの変換で作る: SDK例外のノードの変換はどの案でも要る。AIの例外に入れると、Gemini固有の項目が入り、同じ事実が2つのノードに出る。SQLの変換と同じ形にする。

## 後続の作業

- 変換器の日あたりの判定（`PerDay`のquotaIdで`quota_exhausted`にする分岐）の削除と、A6アラーム・reasonの扱い。無料枠の名残なので、#534の観測で本番のquotaIdを確かめてから行う。有料枠の月の支出上限による429の検知もあわせて考える。
- ほかの工程のログ方針への移行。移行すれば本変換が効く。
- `BadRequest`のfield（本番で付いているのが見えたら）。

## 確認範囲

- 2026-10-02: 公式資料・固定版SDK・当時のコードを照合し、HTTP 200内の429とJSONDecodeErrorの原因表示を合成実行で確かめた。`.env`・秘密情報・実API・本番ログは使っていない。
- 2026-10-03: #524・#527・#529/#532・#534と、ログの3経路を今のコードで確かめた。
- 2026-10-04〜08: 次を確かめた。
  - 既存の変換（SQL・Pydantic・イベント検証）と、診断項目の数え方。
  - ログ方針のロガーの利用箇所と、各入口のimport。ログ方針はGeminiを使わないschedulerと週次トレンドのworkerも読み込む（実装後に`tests/test_lazy_ai_sdk_import.py`で判明）。
  - `google.genai`のimportの負担（手元で0.13〜0.2秒、約26MB）。
  - SDK 2.28.0の`APIError`。
  - 公開されている429の実例（違反は最大4件）と、公式のrate limitsの資料（有料枠の支出上限で429が返る）。
