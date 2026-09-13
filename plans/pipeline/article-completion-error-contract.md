# 共通HTTPエラーと本文補完エラーの実装プラン

Status: タスク1・2の定義はPR #356で実装・マージ済み（2026-09-13）。タスク3のHTTP変換、新経路用HTML抽出と記事の統合・構築処理を実装済み。工程側ハンドラー・新経路への接続は後続タスクとする。

## Problem

新しいイベント駆動経路で、HTTPに関する共通の失敗と、本文補完固有の失敗を情報を落とさず伝えられるようにする。エラーは発生事実を表し、再試行可否・待機・終了の判断は各工程が所有する。

## Evidence

- [本文補完Consumer仕様](../../specs/pipeline/article-completion-consumer.md): 正常終了・失敗の区別、先勝ちの確定、非closed行の救済と受信条件、DBとSQSの責務を定義している。
- [共通HTTP failure](../../backend/app/http/failure.py): `HttpTransportFailure`が通信失敗の段階・理由・proxy statusを持ち、到達可能性は段階から導く。HTTPエラー応答は現在の対象外で、再試行方針を持たない。
- [HTTPクライアント](../../backend/app/http/external.py): 共通の外部通信と既存の保護を提供する。今回の定義タスクでは変更しない。
- [現在の外部取得エラー](../../backend/app/collection/external_fetch_errors.py)と[変換](../../backend/app/collection/external_fetch_error_mapping.py): 旧経路の接続を把握する証拠。`retryable`属性や旧分類を新設計の前提にしない。
- [既存の抽出失敗](../../backend/app/collection/article_completion/scrape_failure.py)と[補完拒否](../../backend/app/collection/article_completion/completion_failure.py): 抽出結果なし、抽出器の異常、品質不足、構築defect等の発生事実を確認する材料。
- [既存AIのエラー](../../backend/app/analysis/curation/errors.py): 発生理由と元の原因を保持する例外の参照元。Taskiqへの接続方法は今回の参照対象にしない。

## Invariants

1. 共通HTTPエラーは全工程が利用する契約とし、補完専用として複製・再分類しない。
2. エラーは`retryable`、再試行上限、待機方針、工程が決めた再試行時刻、`closed`化などの処置を持たない。
3. Retry-Afterは相手の応答情報として保持する。採用可否と次回時刻は工程側で決める。
4. 抽出失敗・補完拒否などの工程固有エラーは本文補完が所有する。
5. 新しいエラーを旧TaskiqのService・変換・ハンドラーへ接続しない。既存の通信failureの利用者にも変更を要求しない。
6. 想定外例外を既知の補完拒否や通信失敗と偽って分類せず、元の原因を保つ。
7. 型だけの段階で新規テストを追加しない。変換処理・ハンドラーの実装時に振る舞いを検証する。
8. エラーはログ基盤に依存せず、フィールドと意味の契約だけを持つ。工程ハンドラーが工程情報を補い、出力側が出力先に応じた項目と形式を選ぶ。所在を付けるためだけに共通HTTPエラーを包み直さない。

## 責務と配置

| 所有者 | 責務 | 保持する情報の例 |
|---|---|---|
| `app/http/` | 全工程共通のHTTP失敗の事実 | `HttpTransportFailure`、取得先のHTTP status、相手のRetry-After、応答受信時刻 |
| `app/collection/article_completion/`のエラー | 抽出・記事完成に固有の失敗の事実 | 抽出結果なし、抽出器の異常、品質条件の不成立、構築defect |
| 各工程のハンドラー | 上記のエラーへの対処 | 再試行、待機、補完の終了、監査への対応付け |
| ログ・メトリクス等の出力側 | 出力先に応じた項目の選択と形式への変換 | 明示的に選択した`CODE`や判断に必要な事実 |

HTTPエラー応答と通信失敗は区別する。取得先が返す403とproxy接続時の403を混同せず、403だけからボット拒否という原因を推測しない。robots等の取得ポリシーや既存のURL・SSRF保護のエラーは、責務に沿って扱い、今回その境界を移動しない。

## タスク1: 共通HTTPエラーを定義する

### 対象と実施内容

- 新規ファイル案は`backend/app/http/errors.py`。共通基底`HttpError`、通信失敗を保持する`HttpTransportError`、HTTPエラー応答を保持する`HttpResponseError`を定義する。名称は実装開始時に既存語彙との衝突を確認する。
- 通信エラーは既存の`HttpTransportFailure`をそのまま保持する。段階・理由・proxy statusを新しいenum等に写し直さない。
- HTTP応答エラーは取得先のstatus、任意のRetry-After、必要な応答受信時刻を持つ。statusだけから再試行判断や補完工程の意味を生成しない。
- Retry-Afterは対象ヘッダーの値を応答事実として保持する案とする。秒数・日時の解釈、待機期限の算出はこの定義タスクに含めない。
- 元の原因例外は後続の呼び出し側が例外チェーンで保持する。本文・ヘッダー全体・秘密情報を保持する汎用payloadは追加しない。出力側が必要な項目を選択し、全フィールドや文字列表現をそのまま送信する前提にしない。

### 完了条件

全工程が参照できるHTTPエラーの定義が揃い、通信・HTTP応答の事実を表せる。工程固有の概念や再試行方針を持たず、旧経路や既存の`HttpTransportFailure`利用者への接続変更がない。

## タスク2: 本文補完固有のエラーを定義する

### 対象と実施内容

- 新規ファイル案は`backend/app/collection/article_completion/errors.py`。補完工程固有の基底エラーと、必要な事象別の例外を定義する。
- 現行コードとドメイン制約から、非HTML・抽出結果なし・抽出器の例外や想定外戻り値・抽出品質不足・記事完成時の既知defectを整理する。既存の原因語彙は、意味が一致する場合に再利用する。
- 「抽出結果がなかった」と「抽出処理が異常終了した」を区別し、後続の工程側判断に必要な根拠を保持する。
- HTTP失敗はタスク1の共通エラーを利用し、補完専用のHTTP例外を作らない。DBエラー等の既存例外も定義し直さない。
- 正常終了の結果型は別タスクにできる。完成・処理済み・競合をこのエラー階層へ含めない。

### 完了条件

補完固有の事象を理由・原因として伝えられ、共通HTTPとの所有境界が明確である。既存の`scrape_failure.py`・`completion_failure.py`や旧Taskiqの呼び出し元を置き換えていない。

## 定義タスクの検証と停止条件

- タスク1・2の段階ではエラー型を確認するためだけの新規テストを追加しない。レビューでは仕様とコードを照合し、事実の欠落、工程の判断の混入、旧経路への接続がないかを確認する。
- 実装変更後は`/check`に従い、該当する既存のlint・format・型・テストの検証を行う。「テストを追加しない」は既存チェックを省略する意味ではない。
- タスク1・2の完了条件を満たしたら停止する。未接続であることは、この定義タスクでは意図した完了状態である。

## 後続タスクの順序

| 順序 | 作業 | その段階で検証する振る舞い |
|---|---|---|
| 3 | 新経路用の変換処理を作る | 通信失敗の詳細・HTTP応答・Retry-Afterを失わず共通エラーへ渡す。対象外例外を既知の拒否へ誤変換しない |
| 4 | 補完工程のハンドラーを作る | 理由別の再試行・終了判断、相手の待機指示の扱いを検証する。判断表はこの段階で確定する |
| 5 | 新経路のService・Consumer・DB確定処理へ接続する | 受信条件、正常終了、先勝ち、closed確定、原子的な完成保存、commit失敗時の扱いを実DBで検証する |
| 6 | SQS・Lambda・relay・救済を接続する | commit後の受信完了、再配信、可視性変更、非closed行の救済、旧経路との切替を検証する |

変換処理のテストは、実際のSDK例外・HTTP応答等を入力にして原因が正しく伝わることを確認する。ハンドラーのテストは、工程の対処が契約どおりになることを確認する。クラスの継承関係・内部属性・ヘルパー呼び出し順を固定するだけのテストは作らない。

## Non-goals

- タスク3のHTTP変換段階での補完固有の抽出処理・変換・工程側ハンドラー・新経路への接続。
- タスク1・2での変換処理、HTTPクライアントへの接続、再試行判断、DB・SQS操作、正常終了型の実装。
- 旧Taskiqへの互換変換や新エラーの接続、既存の共通HTTP利用箇所の一括移行。
- DB schema・依存追加・認証認可・既存の通信保護・公開APIの変更。
- 旧コードの構造維持を理由にした新エラーへの再試行属性・DBスケジュールの持込み。

## Done

タスク1・2は上記の定義と未接続の境界を満たせば完了とする。タスク3のHTTP変換は、以下の契約と振る舞いのテストを満たし、`/check`が成功したら停止する。

## タスク1・2の実装結果

- [共通HTTPエラー](../../backend/app/http/errors.py)に`HttpError`・`HttpTransportError`・`HttpResponseError`を定義した。既存の`HttpTransportFailure`を保持し、応答では`status_code`・`received_at`・任意の生の`retry_after`を保持する。
- [補完固有エラー](../../backend/app/collection/article_completion/errors.py)に`ArticleCompletionError`と、Content-Type不適合・抽出結果なし・抽出異常・品質不足・完成拒否の5種類を定義した。抽出異常は`ArticleExtractionCrashReason`で例外と想定外結果を区別し、完成拒否は既存の`AnalyzableArticleDefect`を保持する。
- どちらも`Exception`を直接継承し、ログ基盤への依存と`SAFE_ATTRS`を持たない。識別用の`CODE`と発生事実のフィールドを保持し、出力項目・形式は出力側が所有する。新経路への接続時に、手書きログ・監査・メトリクスを含む出力側の扱いを確認する。
- 再試行属性・判断処理、変換処理、旧Taskiqへの接続、DB・SQS操作、新規テストは追加していない。元の例外とのチェーンは、後続の変換処理で接続する。
- 検証: `ruff check app/`・`ruff format --check app/`成功、既存単体テスト6,784件成功、`make test-integration PYTEST_ARGS='-x -q'`で既存DB統合テスト1,434件成功。テスト設定で新規2モジュールのimportも確認し、一時DB・Redisの終了を確認した。独立した型チェックコマンドは現行の設定にない。

## タスク3: 共通HTTPの変換処理

### Problem / Evidence

SDKの元例外から共通HTTPエラーへ、工程の判断を混ぜずに事実を渡す。既存の`classify_httpx`とそのテストを通信分類の根拠として利用し、分類表を複製しない。httpxの[例外契約](https://www.python-httpx.org/exceptions/)と[応答・ヘッダーAPI](https://www.python-httpx.org/quickstart/#response-headers)を確認した。

### 実装したインターフェース

[error_mapping.py](../../backend/app/http/error_mapping.py)に次の2関数を追加する。

| 関数 | 入力 | 出力 |
|---|---|---|
| `http_transport_error_from_exception` | 元の通常例外 | `HttpTransportError`、対象外なら`None` |
| `http_response_error_from_exception` | `httpx.HTTPStatusError`、必須の`received_at` | `HttpResponseError` |

- 通信失敗は`classify_httpx`の結果を保持する。詳細不明の通信失敗は既存の`UNKNOWN`、一般例外・宛先拒否・HTTP応答エラーなど対象外は`None`とし、HTTP失敗へ丸めない。今回のSDK対応はhttpxと既存分類が扱うDNS解決失敗とする。
- 応答エラーは取得先のstatusと生の`Retry-After`を保持する。ヘッダー欠如は`None`、空文字・空白・日時形式・不正値は解釈せず維持する。proxy拒否は通信failureの`proxy_status`とし、取得先の応答と区別する。
- `received_at`は呼び出し側が応答受信直後・ステータス検証前に記録したタイムゾーン付きUTC時刻を受け取る契約とする。変換側で現在時刻の取得・補完・正規化や待機期限の計算はしない。
- 変換関数はエラーを返し、送信・raise・ログ出力はしない。後続の呼び出し側は`raise mapped_error from original_error`で原因をつなぎ、変換対象外は元の例外を伝播させる。最終的に未分類の通常例外は工程ハンドラーで扱う。キャンセル・プロセス終了は通常失敗の変換対象外とする。
- 旧処理は残し、新しい処理を独立して実装する。新経路の完成・切替後に旧処理を削除する。

### 検証と残作業

新規テストは実際のhttpx例外・応答を入力に、通信情報・status・受信時刻・待機指示の保持と対象外の誤分類防止を検証する。既存分類の網羅テストや継承・文字列表現だけのテストを重複させない。

HTTP実行時の時刻取得、元例外とのチェーン、工程判断と出力先への接続は後続の実装時に検証する。今回の変換テストを、それらの接続検証済みとは扱わない。

検証結果（2026-09-13）: Ruff lint・format成功、新規22ケースを含む単体6,842件成功、`make test-integration PYTEST_ARGS='-x -q'`でDB統合1,434件成功。一時DB・Redis・networkの削除を確認した。専用の型チェックコマンドは現行設定にない。HTTP変換の実装時点でローカルmainに追加した共通HTTP定義は、PR #356の承認済みコミット`be07b4f39`と同一であり、補完固有の定義はその後のmain同期で取り込んだ。

## タスク3の続き: HTML抽出処理と共有素材（2026-09-14）

### Problem / Evidence

抽出素材の段階でタイトル必須・本文の最低文字数を検証すると、観測値との統合前に補完を打ち切ってしまう。既存`scraper.py`・素材の生成テスト・`complete_with_html()`・`AnalyzableArticle.build_or_reject()`と[Trafilaturaの公式API](https://trafilatura.readthedocs.io/en/latest/corefunctions.html#bare-extraction)を確認し、抽出と完成条件の判定を分けた。

### 実装内容とInvariants

- `content.py`へ`RawResponse`と不変の`ScrapedContent`を移動し、旧`scraper.py`から再公開する。素材の品質検証と失敗値を返す`try_create()`を削除し、整形のみの`from_extraction()`に置き換える。タイトル整形・500文字への切り詰め・本文の前後空白除去・公開日時変換を維持する。
- 旧スクレイパーも共有生成処理を使う。短い本文・タイトル欠落で早期終了せず統合・構築へ進む変更を許容し、完成記事の条件とソース別採用方針は維持する。
- `html_extraction.py`の同期関数`extract_html_content(raw)`は`text/html`のみを受け入れ、結果なし・想定外結果・抽出器の通常例外を定義済みエラーで区別する。例外チェーンを維持し、捕捉範囲は抽出器の呼び出しに限定する。
- 文字コードはHTTP指定優先、先頭2,048バイトのHTML指定、`decoded_text`への代替という既存方式を維持する。新処理はログを出さず、旧処理のログ方針を変えないためデコード処理は当面独立して持つ。
- 抽出設定を維持し、Documentの項目不足は素材として返す。`ArticleContentQualityError`は使用しない。

### テストの整理

- 素材が空タイトル・短い本文を拒否するテストと、抽出品質不足の本文断片を固定するテストを削除した。タイトル整形・公開日時変換の保証は`test_content.py`へ移した。
- 既存の文字コードテストを新旧のデコード処理へ適用した。旧スクレイパーの実抽出・旧エラー処理の独立した保証は維持する。
- 新関数は実HTML（UTF-8 / Shift_JIS）、項目不足、入力不適合、抽出結果と例外の区別・原因保持を検証する。抽出器以外の例外とキャンセル・プロセス終了を変換しないことも確認する。
- 完成記事の本文長・必須項目は既存構築テストが所有し、同じ条件の網羅テストや継承・内部呼び出し順・引数一覧だけのテストは追加しない。

### Non-goals / Done

HTTP取得・SQS・再試行判断・DB保存・ソース別補完方式の見直し・記事構築拒否の新経路への接続は行わない。旧エラーの一括撤去も行わない。ローカルmainで既存の未コミット変更を保持し、上記の契約と旧Taskiqへの新エラー未接続を確認して、`check`のlint・format・単体・DB統合テストが成功したら停止する。

### 検証結果

Ruff lint・format（アプリと変更テスト）成功、対象55件成功、単体6,870件成功、`make test-integration PYTEST_ARGS='-x -q'`でDB統合1,434件成功。一時DB・Redis・networkの削除を確認した。`pytest tests/ -x -q`は未起動DBへ接続して停止したため、単体は`-m 'not integration'`で分離し、DB統合は上記の専用起動経路で全件実行した。新エラーの参照元は追加した抽出モジュールのみで、旧Taskiqへの接続はない。

## タスク3の続き: 記事の統合・構築（2026-09-14）

### Problem / Evidence

観測値とHTML素材から完成記事を構築する新経路を、旧Taskiqの実行状態から独立させる。既存の`complete_with_html()`、ソース別ポリシー、`build_or_reject()`と構築テストを確認した。構築結果の`QualityTooLow`が持つ`unmapped`を新エラーでも保持し、変換で理由の詳細を落とさない。

### 実装内容とInvariants

- `html_completion.py`に同期関数`complete_with_html(observed, completion_policy, html, *, source_id, source_url) -> AnalyzableArticle`を追加した。`ReadyForArticleCompletion`や旧`completer.py`の実行処理は利用しない。
- 既存`ArticleCompletionPolicy.resolve()`へ観測値と素材を渡し、統合結果を`AnalyzableArticle.build_or_reject()`で構築する。値の採用規則・完成条件を複製しない。
- 完成記事はそのまま返し、`QualityTooLow`だけを`ArticleCompletionRejectedError`へ変換して投げる。エラーに`unmapped: tuple[str, ...] = ()`を追加し、`defects`とともに順序・内容・重複を保持する。
- 未分類の検証結果と想定外例外を区別する。その他の例外は捕捉せず、構築結果から疑似的な原因例外を作らない。新関数・エラーに再試行判断やログ出力を追加しない。既存構築処理内の未分類検証ログは変更しない。

### テストとNon-goals / Done

実ポリシー・構築処理による完成の代表2例（HTMLタイトル欠如・HTML優先）、既知の構築拒否、複数defectと未分類詳細の保持、統合・構築の想定外例外の伝播を検証する。完成条件やポリシーの網羅テストを重複させず、旧経路の独立した保証は残す。

DB状態の確認・HTTP取得・SQS・再試行・closed化・監査・保存・Consumerへの接続は行わない。ローカルmainで既存の未コミット変更を保持し、仕様更新と旧Taskiqへの新エラー未接続を確認したうえで、Ruff lint・format、単体（`-m 'not integration'`）、`make test-integration`が成功したら完了とする。

### 検証結果

Ruff lint・format成功、新経路6ケースと旧Completerの2ケースが成功。単体6,882件成功（`pytest tests/ -m 'not integration' -x -q`）、DB統合1,434件成功（`make test-integration PYTEST_ARGS='-x -q'`）。一時DB・Redis・networkの削除を確認した。旧Completer・Service・Readyには差分がなく、新しい構築拒否エラーの利用は新関数に限定されている。
