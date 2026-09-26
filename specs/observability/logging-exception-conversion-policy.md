# 例外の変換と値の検査の責任分担

作成: 2026-09-25
Status: Draft
Implementation: 未実装

[共通基底ポリシー](./logging-base-policy.md)の例外の抽出・原因チェーン・例外項目の上限と予算を置き換える差分仕様。本書の範囲では本書を優先する。原因チェーンの形式を記述している[アプリケーションログの概念別ポリシー](./application-logging-policy.md)と[AI分析のログポリシー](./ai-analysis-logging-policy.md)の該当箇所も、本書で置き換える。

## Problem

例外を共通の形式へ変換する処理と、ログの値を検査する処理の責任が混ざっている。

- 値の検査が、例外の形式に合わせた深さの上限（`EXCEPTION_DEPTH_LIMIT = 19`）を持ち、`depth_limit` 引数で通常の値と例外の値を切り替えている。19は例外側の原因の段数（8）と `error_details` の形から逆算した値で、例外側の上限を変えると手で合わせる必要がある。
- 例外の出力が通常の項目と同じ予算（256件・16000文字）を使う。原因ごとに frames を持つため、連鎖した例外では frames だけで予算を使い切り、`event` を含むログ全体が `log_policy_budget_exceeded` に置き換わる。
- 原因は常に1件なのに `causes` を配列で持ち、1段ごとに深さが2増える。
- `__cause__` と `__context__` を区別せずに出力している。
- `ApplicationError.details` の型が入れ子を許し、例外側が出力の形を保証できない。

## Evidence

- [extraction.py](../../backend/app/log_policy/exceptions/extraction.py): `CAUSE_DEPTH_LIMIT = 8`・`EXCEPTION_LIMIT = 32`・1例外あたりの `FRAME_LIMIT = 50` を持つ。原因は `causes` に1件の配列として入れ子にし、グループのメンバーは `exceptions` に入れる。
- [value_preparation.py](../../backend/app/log_policy/value_preparation.py)・[processor.py](../../backend/app/log_policy/processor.py): 通常の値には `DEPTH_LIMIT = 10`、例外の値には `EXCEPTION_DEPTH_LIMIT = 19` を引数で渡す。例外の項目は通常の項目と同じ `LogEventBudget` で数える。
- [errors.py](../../backend/app/shared/errors.py): `ApplicationErrorValue` は辞書・配列の入れ子を許す。`details` を作る3か所（SQS入力・AI provider・assessment）はすべて平らな辞書で、`details` を読むのはログの変換だけである。
- [sql.py](../../backend/app/log_policy/exceptions/sql.py): SQL例外はドライバーの原因を変換で集約し（`cause_is_aggregated`）、それより内側をたどらない。
- 実測（2026-09-25。processor・値の検査を直接呼び、frame 数は再帰関数で作った概算）:
  - 原因が約38 frame のとき、外側が約22 frame 以下なら正常に出力し、約24 frame 以上でログ全体が `log_policy_budget_exceeded`（`value_count`）になった。
  - 深さ10の検査で欠けずに残る原因は、今の形式（配列で入れ子）で3段、辞書で入れ子にした場合で7段だった（`error_details` に検証エラーの `issues` を入れた場合）。
  - httpx の接続失敗は `httpx.ConnectError ← httpcore.ConnectError` の1段だった。
  - アプリのコードに `ExceptionGroup` を作る処理（`asyncio.TaskGroup`・`except*`）はない。
  - 同じ呼び出しの深さで、連鎖なしの frame 数は45、連鎖ありの合計は46だった。原因の frames は捕まえた位置から投げられた位置までだけを持つため、連鎖した例外どうしで呼び出しの深さを分け合う。
  - 実際に近い2件の連鎖（合計46 frame）の出力は、今の値の検査の数え方で199件・5,069文字だった。
- [基底ポリシーの frame 数の実測](./logging-base-policy.md#例外frame数と走査件数の上限を実測で調整2026-09-19): route で捕まえた場合は41〜42 frame、全体の例外ハンドラで捕まえた場合は62 frame だった。
- `error_details` の項目数は、アプリの例外で最大3、SQL例外で最大7、イベントの検証エラーで最大約27（違反項目が最大8つ）。

## 責任分担

| 担当 | 責任 | 持たないもの |
| --- | --- | --- |
| 例外の変換 | 例外の連鎖をたどり、共通の形式へ変換して返す。自分の上限を持ち、上限・循環で止めたことを出力の中で示す。出力が値の検査の上限に収まることを保証する。 | 出力の検証、ポリシー（deny・mask・sanitize）の適用 |
| 値の検査 | 呼び出し側の値と例外の出力を、同じ規則で検査する。ポリシーと情報漏洩防止を適用する。 | 例外の形式についての知識 |
| processor | 例外の変換を呼び、その出力を通常の項目と別の予算で値の検査に通す。 | 例外の形式についての知識 |

- 値の検査で deny による除外・mask・sanitize・情報漏洩防止の置換が起きるのは、正常な動作とする。
- 例外の出力が値の検査の上限（深さ・文字数・件数）に当たるのは、例外の変換の不具合とする。その場合も、値の検査は通常どおり置き換える（[予算](#予算)を参照）。

## 出力形式

外側の例外（ログに渡した例外）はトップレベルに置く。関連する例外は入れ子にせず、`related_exceptions` に1つの配列として並べる。

例外1件の共通形式:

| 項目 | 内容 |
| --- | --- |
| `error_class` | 型の完全修飾名 |
| `error_message` | 変換済みの原因文 |
| `frames` | file・function・line の配列。値・ソース行は含めない。 |
| `error_details` | 任意。例外の種類ごとに型で決めた診断情報。 |

`related_exceptions` の各要素:

| 項目 | 内容 |
| --- | --- |
| `parent` | 関係元の例外。`related_exceptions` の中の要素なら0始まりの位置、外側の例外なら `null`。 |
| `relation` | `cause`（`__cause__`）、`context`（`__context__`）、`member`（`ExceptionGroup` のメンバー）のいずれか。 |
| `exception` | 例外1件の共通形式。上限で止めた場合は `"[limit]"`、循環の場合は `"[cycle]"`。 |

- 全要素が同じキーを持つ。省略は、値の位置を固定マーカーに置き換える値の検査と同じ表し方にする。
- 要素の番号は持たない。並び順で親は必ず子より前にあるため、`parent` は位置で示す。
- 並び順は外側からの深さ優先とする。各例外について、前の例外（`cause` / `context`）を先に、メンバーを元の順序で後に並べる。
- `__cause__` を優先し、`__cause__` がなく `__suppress_context__` が偽の場合だけ `__context__` をたどる。`raise ... from None` を尊重する。
- `exception` が `"[limit]"` の要素は、その関係から先を省略したことを示す。`relation` が `member` の場合は、同じ `parent` の残りのメンバーをまとめて省略し、以降は列挙しない。
- `exception` が `"[cycle]"` の要素は、たどっている経路上の例外に戻る参照を示す。共有参照は循環として扱わない。

上限で止めた場合の例:

```json
{
  "error_class": "app.shared.errors.ApplicationError",
  "error_message": "article fetch failed",
  "frames": [{"file": "app/collection/fetch.py", "function": "fetch", "line": 42}],
  "error_details": {"reason": "network"},
  "related_exceptions": [
    {"parent": null, "relation": "cause", "exception": {"error_class": "httpx.ConnectError", "error_message": "...", "frames": []}},
    {"parent": 0, "relation": "cause", "exception": {"error_class": "httpcore.ConnectError", "error_message": "...", "frames": []}},
    {"parent": 1, "relation": "context", "exception": "[limit]"}
  ]
}
```

## error_details

- 例外の種類ごとに型で形を決める。SQL例外は `PostgresErrorDetails`、イベントの検証エラーは `EventValidationDetails`、アプリの例外は `ApplicationError.details` とする。
- `ApplicationErrorValue` を `str | int | float | bool | None` に絞る。構造を持つ診断情報が必要な例外には、専用の変換と `TypedDict` を用意する。

## 上限

### 例外の変換

例外の変換の上限は、調査に必要な量と実際の連鎖の長さから決め、値の検査の上限から逆算しない。値の検査の上限に収まることは、テストで確認する。

| 上限 | 値 | 超えたとき |
| --- | --- | --- |
| 例外の件数 | 8 | その関係の位置に `exception` が `"[limit]"` の要素を置き、それより先はたどらない。 |
| frame 数の合計 | 64 | 収まらない例外の `frames` を全体 `"[limit]"` にし、部分リストを残さない。 |
| 1つの文字列の長さ | 4000文字 | その文字列を `"[limit]"` にする。 |
| 文字数の合計 | 16000文字 | 収まらない文字列を `"[limit]"` にする。 |
| `error_details` の項目数の合計 | 32 | 収まらない例外の `error_details` を全体 `"[limit]"` にする。 |

- 1つの文字列の長さ以外は、例外全体の合計で数える。合計は出力の並び順（外側からの深さ優先）に使い、収まらない部分だけを置き換えて、残りは出力する。
- 件数には、変換した例外（外側を含む）と、循環として省略した参照を数える。上限で省略した要素は数えない。
- 文字数に数えるのは、例外の変換が出力する文字列（型名・原因文・file・function・`error_details` のキーと文字列の値）とする。出力の構造を表す固定のキー名は数えない。
- `error_details` の項目数は、`error_details` の中のキーと配列の要素を数える。
- `error_details` のキーは置き換えられないため、値より先に数える。キーが1つの文字列の上限を超えるか、文字数の合計の残りに収まらない場合は、`error_details` 全体を `"[limit]"` にする。
- 原因の段数の上限（`CAUSE_DEPTH_LIMIT`）と1例外あたりの frame の上限（`FRAME_LIMIT`）は廃止する。件数の上限でたどりを終わらせ、frame は合計で制限する。

値の根拠:

- 件数: 実際の連鎖（外側を含めて2〜4件）の2倍程度。
- frame 数の合計: 連鎖しても合計は呼び出しの深さでほぼ決まるため、既存の実測で最も深い全体の例外ハンドラ（62）まで収まる値にする。
- 1つの文字列の長さ: 値の検査の単一文字列の上限と同じ。原因文は典型的には数十〜数百文字、長い検証エラーの要約で約1700文字。
- 文字数の合計: 通常の項目の予算と同じ。実際に近い2件の連鎖で約5,000文字。
- `error_details` の項目数: 今の最大（約27）を収める値。

### 値の検査

- 深さの上限は `DEPTH_LIMIT = 10` に統一し、`depth_limit` 引数と `EXCEPTION_DEPTH_LIMIT` を廃止する。
- 例外の出力にも、通常の値と同じ規則（単一文字列の上限、型、循環など）を適用する。

## 予算

- processor は、通常の項目と診断を数える予算と、例外の出力を数える予算を分ける。
- 例外の予算の大きさは、例外の変換の上限いっぱいの出力が収まる値とし、実装時にその出力から求める。概算は約400件・約18,000文字（文字数の合計16000文字に、構造を表すキー名が加わる）。
- 通常の予算を超えた場合は、今まで通りログ全体を固定出力に置き換える。
- 例外の予算を超えた場合（例外の変換の不具合）は、例外から作った項目（`error_class`・`error_message`・`frames`・`error_details`・`related_exceptions`）をすべて `"[limit]"` に置き換え、`_policy_limited: true` を付ける。通常の項目と診断は残し、例外の項目の一部だけを残すことはしない。

## Invariants

- 例外の出力にも、選択したポリシーの deny・mask・sanitize と情報漏洩防止を適用する。
- SQLの実データと検証入力を出さない。
- frames に値・ソース行を含めない。
- 例外の型判定・属性取得の前に、上限と循環を確認する。
- 例外の変換・値の検査が失敗しても業務へ例外を伝播させず、原文へフォールバックしない。
- 通常の項目の選別・予算・置換の契約は変えない。
- 出力の構造を表すキー名（`related_exceptions`・`parent`・`relation`・`exception`）は、deny・mask の対象と重ならない名前にする。
- 呼び出し側が例外専用の項目名（`error_details`・`related_exceptions`）を渡しても受け入れない。

## Non-goals

- SDK固有の原因構造（`provider_error` など）の取得。
- 値の検査を「構造の検査」と「ポリシーの適用」に分けること。
- 最終JSONのバイト数の上限。
- 全体の例外ハンドラでの `exc_info` 付きログへの対応。

## Done

- 例外の変換が、本書の形式（`related_exceptions`・`relation`・省略の要素）を返す。
- 例外の変換が本書の上限（件数8・frame 数の合計64・1つの文字列4000文字・文字数の合計16000文字・`error_details` の項目数の合計32）を持ち、`CAUSE_DEPTH_LIMIT` と `FRAME_LIMIT` がなくなっている。
- `depth_limit` 引数と `EXCEPTION_DEPTH_LIMIT` がなくなり、深さの上限が10に統一されている。
- 例外の出力が、通常の項目と別の予算で数えられている。
- `ApplicationErrorValue` が平らな値に絞られ、型チェックが通る。
- 次をテストで確認している。
  - 例外の変換の上限いっぱいの出力を値の検査に通しても、上限のマーカーが出ない。
  - 実測で予算を超えた連鎖（原因 約38 frame・外側 約24 frame）でも、ログ全体が置き換わらない。
  - `cause`・`context`・`member` の区別、`raise ... from None`、上限と循環の省略の要素。
  - 合計の上限を超えたとき、収まらない部分（`frames`・`error_details`・文字列）だけが `"[limit]"` になり、他の情報は残る。
  - 例外の予算を超えたとき、例外の項目だけが `"[limit]"` になり、通常の項目が残る。
- 基底ポリシー・概念別ポリシー・AI分析ポリシーの該当箇所に、本書を優先する旨を記載している。
