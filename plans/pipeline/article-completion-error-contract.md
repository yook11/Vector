# 共通HTTPエラーと本文補完エラーの実装プラン

Status: タスク1・2の定義はPR #356で実装・マージ済み（2026-09-13）。タスク3のHTTP変換、新経路用HTTP取得・HTML抽出・記事の統合と構築を実装済み。工程側の純粋な失敗分類・Retry-After解釈を実装済み。失敗ハンドラー・Consumer・DB確定を実装済み。SQS・Lambda接続は[配送仕様](../../specs/pipeline/article-completion-delivery.md)のスライスに従う。

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

## タスク3の続き: 記事補完用HTTP取得（2026-09-14）

### Problem / Evidence

取得済みHTMLを扱う新経路に対し、共通HTTPエラーを使って応答を取得する入口を追加する。旧スクレイパーのrobots判定・サイズ上限・文字コード処理、共通HTTP変換と宛先保護、[HTTPXのストリーミングAPI](https://www.python-httpx.org/async/#streaming-responses)と[Pythonの期限API](https://docs.python.org/3.13/library/asyncio-task.html#asyncio.timeout)を確認した。全量受信後のサイズ判定を、新経路では受信途中の制限にする。

### 実装とInvariants

- `article_fetch.py`の非同期関数`fetch_article_response(url: SafeUrl) -> RawResponse`を追加する。既存の外部HTTPクライアントを1試行内で共有し、robots確認から記事受信まで実行する。リダイレクト非追従・内部リトライなし・キャッシュなしとし、既存の通信保護を維持する。
- robotsの2xxを解析し、許可または404なら記事を取得する。404の本文は読まず、他の非成功応答と確認失敗では記事へ進まない。明示的な禁止とHTTPの403を区別する。
- 共通の受信処理でrobots・記事の本文を10MiBに制限する。Content-Length超過での早期拒否と、64KiB単位の展開後本文の累積確認を行う。上限ちょうどを許容し、超過チャンクを保持せず中断する。本文量の制限をSDK内部の圧縮展開も含む厳密なメモリ上限とは扱わない。
- 通信待ち時間・取得全体の期限はrobots 10秒、記事30秒とする。記事の時計はrobots確認後に開始し、期限切れやキャンセルでも応答とクライアントを閉じる。
- `RobotsDisallowedError`、`ResponseSizeLimitExceededError`、`FetchDeadlineExceededError`を追加する。取得していたものは`resource: FetchResource`（`ROBOTS_TXT` / `ARTICLE_PAGE`）、サイズ判定の根拠は`size_basis: ResponseSizeBasis`（`DECLARED_CONTENT_LENGTH` / `RECEIVED_DECODED_BODY`）で表し、上限・確認サイズ・制限秒数を必要なエラーに保持する。再試行判断・出力処理・本文断片は追加しない。
- 共通HTTP変換・元例外を保持し、ヘッダー受信直後のUTC時刻をHTTP応答へ渡す。自身のタイマー以外のTimeoutError、HostBlockedError、対象外例外、外部キャンセルはそのまま伝播する。
- RawResponseに応答情報と展開後本文を保持し、HTTPXの文字コード選択・置換処理を維持する。HTML受け入れとmeta charsetの処理は抽出側に任せる。

### 重要なテスト / Non-goals / Done

新規テストはrobotsによる取得制御、受信途中のサイズ制限、失敗の誤変換防止、取得期限と資源解放の4保証に限定する。HTTPXの実応答・非同期ストリームを使い、ネットワークはモックする。既存の通信分類表やエラー定義だけのテストを重複させない。期限はテスト内で短くし、10秒・30秒の実時間待機はしない。

旧Taskiqの取得・抽出・構築処理は変更しない。新しい取得・抽出・構築の連結、再試行・監査・DB・SQS・Consumerへの接続は後続とする。ローカルmainで既存の未コミット変更を保持し、仕様更新、Ruff lint・format、単体とDB統合テスト、旧経路への未接続を確認して完了とする。

### 検証結果

Ruff lint・format成功、新規の重要な16ケース成功、単体6,898件成功（`pytest tests/ -m 'not integration' -x -q`）、DB統合1,434件成功（`make test-integration PYTEST_ARGS='-x -q'`）。一時DB・Redis・networkの削除を確認した。旧スクレイパー・Service・HTML抽出・記事構築に差分はなく、新しい取得関数と取得固有エラーは旧Taskiqへ接続していない。

取得制限の情報名は、原因との混同を避けるため`target`から`resource`（`ROBOTS_TXT` / `ARTICLE_PAGE`）、`size_source`から`size_basis`（`DECLARED_CONTENT_LENGTH` / `RECEIVED_DECODED_BODY`）へ変更した。型名も`FetchResource`・`ResponseSizeBasis`へ揃えた。このフィールド名変更ではエラー名・CODE・継承・動作を変更せず、新規テストも追加していない。変更後のRuff lint・format、単体6,898件、DB統合1,434件が成功し、一時環境の削除を確認した。取得関連の3エラーと2つのenumはArticle接頭辞を外し、失敗内容を表す名前へ変更した。補完工程を示すArticleCompletionErrorの継承とCODEは維持し、共通基盤への移動や動作変更は行わない。

最終命名への変更後もRuff lint・format、単体6,898件、DB統合1,434件が成功し、一時DB・Redis・networkの削除を確認した。

## タスク4: 補完工程の失敗判断とRetry-After解釈（2026-09-14）

### Problem / Evidence

新経路のエラーから、原因情報を保持したまま工程の再試行・終了と待機を決める。共通HTTP・補完エラー、AI分析の分類と副作用の分離、本文補完Consumer仕様、HTTP RFCとPython標準日時解析APIを確認した。旧Taskiqの分類や監査基盤への依存は持ち込まない。

### 実装内容 / Invariants

- `consumer_failure_classification.py`に同期の純粋関数`classify_completion_failure(exc: Exception, *, now: datetime) -> RetryArticleCompletion | CloseArticleCompletion`を追加する。戻り値にユニオンを直接記述し、別名・共通基底・actionフィールドは追加しない。不変の両結果型はcodeとrequires_investigationを持ち、RetryArticleCompletionだけがretry_atを持つ。指定なし・無効・0秒・期限経過済みでNoneになることをコメントに明記する。
- HTTP応答と補完固有の確定分類は[Consumer仕様](../../specs/pipeline/article-completion-consumer.md#補完工程の確定した失敗判断)を正本とする。407・511・425、範囲外応答、未分類・抽出異常を調査対象とし、プロキシの拒否を記事側のHTTP拒否へ流用しない。
- 構築拒否は既知・未分類が混在しても、未分類・空の拒否理由があれば再試行を優先する。元例外、原因チェーン、defects、unmappedを変更しない。
- 再試行対象のHTTP応答だけでRetry-Afterを解釈する。秒数は応答受信時刻基準、HTTP日時は旧形式も含めてUTC化する。経過済み・0秒・不正・表現範囲外は追加待機なし、有効な未来時刻は短縮しない。内部の時計・ログ・通信は不要。
- 調査の記録・通知・緊急度は出力側で扱う。DLQ移動後も非closed行の救済再投入を許し、停止や累積試行上限を追加しない。

### Non-goals / Done

DB更新・SQS操作・監査・ログ・通知・Consumer接続・正常終了型の追加は行わない。既存エラーの責務・旧Taskiqの動作を維持する。今回はローカルmainを変更せず、専用worktreeと`codex/completion-failure-classification`で作業し、既存のbackfill等の未コミット変更を保持する。

テストはHTTP判断表、誤った終了の防止、待機時刻、原因情報の保持に絞り、通信分類・完成条件の網羅や型定義だけの保証を重複させない。Ruff lint・format、単体（`-m 'not integration'`）、`make test-integration`を実行し、一時環境の削除と旧Taskiqへの未接続を確認したら、日本語コミット・PRを作成して完了とする。

### 検証結果

Ruff lint・format（app全体と追加テスト）成功。追加64ケース成功、単体6,932件成功（integration 1,434件を分離）、`make test-integration PYTEST_ARGS='-x -q'`でDB統合1,434件成功。一時DB・Redisコンテナとnetworkの削除を確認した。新関数のproduction参照は定義元のみで、旧Taskiq・既存エラー・DB・SQSへの接続変更はない。

### 結果型の分離

RetryArticleCompletionとCloseArticleCompletionに分け、戻り値にユニオンを直接記述する形へ修正した。再試行結果だけにretry_atを持たせ、Noneになる理由をフィールドと解釈関数のコメントへ追記した。既存64ケースの期待結果を更新し、新規テストは追加していない。修正後もRuff lint・format、単体6,932件、DB統合1,434件が成功し、一時環境の削除を確認した。

## タスク5の先行テスト: ConsumerからDB確定まで

### Problem / Evidence / Invariants

新経路のConsumerを接続した際の外部から観測できる振る舞いを、production実装より先にローカルテストへ定義する。既存のAssessment/Curationローカルテスト、migration適用済みDBの共通fixture、Collect権限、補完の原子的保存と確定した失敗判断を確認した。

Consumerの入口は未完成記事IDで、完成・追加処理不要・失敗を区別する。失敗は元例外と工程判断を一緒に結果値で返す（error / decision）。終了判断ではclosed確定後に失敗結果を返し、closedのDB確定に失敗した場合は再試行結果にする。失敗監査の二次障害は元のエラー・待機時刻・確定済みclosedを覆さない。同URLの完成記事が既存なら未完成行だけを削除し、既存記事・監査・Outboxを増やさない。

テストの配置と各保証は[ローカルテストREADME](../../backend/local_tests/README.md#completionのconsumer接続テストファースト)を参照する。HTTP以外の業務処理やDB結果を模倣せず、別接続から確定状態を確認する。並行処理の保証は実ロック待ち、ロールバックは実SQL障害で確認する。

### Non-goals / Done

今回は`backend/local_tests/completion/`と説明文書だけを追加し、本体・仮Consumer・DB schema・既存Taskiq・SQS接続は変更しない。`codex/completion-consumer-tests`の専用worktreeで実施する。全シナリオをcollectionでき、環境不備ではなくConsumer未実装で失敗することを確認し、既存の単体・DB統合が通り、一時環境の削除を確認した時点で停止する。本体を追加してGREENにする作業は次のタスクとする。

### 先行テストの検証結果

17ケースをcollectionでき、実DBの準備後に全件が`ModuleNotFoundError: app.collection.article_completion.consumer`で失敗するREDを確認した。設定不足やseed SQLの型不一致は修正済み。Consumer未実装のため、保存・競合・ロールバックの期待結果が成立することはまだ検証していない。

Ruff lint・format（app全体と追加local_tests）成功、既存単体6,945件成功、`make test-integration PYTEST_ARGS='-x -q'`のDB統合1,475件成功。新規ローカルテストは対象ディレクトリを実行し、既存local_tests一式は再実行していない。ローカルテストと統合テストの一時DBコンテナ・networkの削除を確認した。productionコード・DB schema・既存Taskiqに差分はない。

### 先行テストの配置整理

ユーザー指定により未コミット変更をローカルmainへ移し、補完のconftestに混在していた環境準備と制御処理を分離する。共通DBのfixture、補完の17ケース、HTTP・コミットの制御を根拠とし、期待結果・DB権限・停止条件・後片付けを維持する。

共通のHTTP応答差し替えと記録は`local_tests/http.py`、補完の応答待機は`completion/http_control.py`、確定直前の停止と実SQL障害は`completion/commit_control.py`へ置く。補完のconftestには、独立した設定fixtureと各道具の組み立て・後片付けを残す。DB準備の共通基盤、他工程、本体、schemaは変更せず、テストケースは増減しない。配置整理後も全17ケースを収集でき、Consumer未実装だけを理由に失敗することと既存単体・DB統合の成功を確認して完了とする。

整理後のRuff lint・format、既存単体6,975件、DB統合1,475件が成功した。補完17ケースは全件Consumer未実装のModuleNotFoundErrorで失敗し、保存・競合の期待結果は未検証のままである。共通HTTPの応答・記録と停止・再開・後片付けは個別に動作確認した。既存local_tests一式は再実行していない。ローカルテストとDB統合の一時コンテナ・networkの削除を確認し、変更はローカルmainに未コミットで保持する。


## タスク5の本体実装: ConsumerとDB確定

### Problem / Evidence / Invariants

既存の取得・抽出・構築・失敗分類を、新経路のConsumerとDB確定へ接続する。合意済みConsumer仕様、先行17ケース、既存完成記事Repository・監査payload、caller管理セッションとDB例外変換、Collect権限を確認した。

Consumerは未完成記事IDを受け取り、既存CompletionSucceeded、理由付きCompletionNotRequired、元例外と工程判断を保持するCompletionFailedのユニオンを直接返す。新経路専用Repositoryは非closed条件でDELETE／UPDATEを競合させ、先行commitを維持する。成功時は記事保存・未完成行削除・成功監査・Outboxを一括commitする。URL競合は未完成行だけ削除する。再試行は行を変更せず、終了はclosed確定後に失敗結果を返す。

HTTP待機前に読取セッションを閉じ、DB障害は既存のセッション境界で変換する。失敗監査は別トランザクションで行い、その通常例外を元の結果へ混ぜない。監査は明示した情報だけを既存payloadへ写し、例外の自由文・本文・生のヘッダーは追加出力しない。外部キャンセルは通常失敗へ変換しない。

### Non-goals / Done

ローカルmainの先行テストと既存変更を保持する。旧Taskiq・DB schemaと権限・ソースポリシー・失敗分類表・エラー定義を変更しない。SQS・Lambda・救済・通知・メトリクス・追加の全体期限は対象外とする。先行17ケースへ正常終了理由の確認を足し、新規ケースは初回DB照会障害と外部キャンセルの2つだけとする。Ruff lint・format、単体、DB統合、ローカルテスト全体の成功と一時環境の削除を確認して、未コミットで完了する。

### 抽出キャッシュによるテスト間干渉

実Consumerを接続すると、別ケースで同じサンプルHTMLを抽出した履歴が残り、保存テストが抽出拒否で終了した。インストール済み実装と[公式の重複判定仕様](https://trafilatura.readthedocs.io/en/latest/deduplication.html#clearing-the-cache)を確認し、補完fixtureで各ケースの前後にreset_caches()を呼んで分離した。同一ケース内の抽出・重複除去とproduction設定は維持し、抽出結果のモック化や期待条件の緩和は行っていない。長寿命の実行環境におけるキャッシュ寿命の見直しは配送接続時の検討事項として仕様へ残す。

### 本体実装の検証結果

Ruff lint・formatが成功。補完19ケースが実DBで成功し、単体6,975件（integration 1,475件を分離）、`make test-integration PYTEST_ARGS='-x -q'`のDB統合1,475件、`make test-local`の全101件（補完19件を含む）が成功した。ローカルテストとDB統合の一時コンテナ・networkの削除を確認した。旧Taskiqの実行経路、DB schema・権限、依存ファイルに差分はなく、変更はローカルmainに未コミットで保持する。

## 後続: SQS・Lambda配送の実装スライス（2026-09-14）

[配送仕様](../../specs/pipeline/article-completion-delivery.md)を設定と受け入れ条件の正本とする。Lambdaの実行期限は10分、最大10件の逐次処理、残り75秒未満で次の記事を開始しない。1件全体の期限は追加しない。分類関数のretry_atは短縮せず、配送側だけで個別待機を最大11時間へ制限する。

1. 起動時の不要な設定依存を解消し、補完に必要な資源の初期化・解放を実装する。
2. 記事内の重複除去を維持しながら、別記事・過去試行から抽出を独立させる。
3. 既存イベントの入力検証とConsumerの結果を、受信完了・部分バッチ失敗へ接続する。
4. 待機変更・元時刻と適用値の記録・残り時間確認・未着手分の再配信を実装する。
5. AWS設定と配送経路を検証する。通常可視性は3,600秒以上とし、DLQ等の残る運用値を具体化する。

各スライスで重要な振る舞いのテストを先に置き、実装・検証後に次へ進む。配送ログは各処理と一緒に追加する。既存のConsumer・品質条件・分類表のテストを重複させない。起動時の設定依存を仮の環境変数で回避せず、旧Taskiq・DB schema・既存エラーの責務を維持する。

本追記は仕様作成のみであり、コード・テスト・AWS設定は変更していない。救済投入・旧経路の切替と、本番への適用は実装完了と分けて扱う。

### 配送スライス1: 記事取得工程の共有起動・資源管理

Problem: 補完の起動で不要な全体設定を要求せず、外部から記事を取得する工程で共有できる資源の所有境界を作る。

Evidence: AIの既存ライフサイクル、補完Consumerのセッション生成契約、取得ツールのimport時設定依存、既存HTTP設定・IAM・DBエンジンを確認した。

Invariants: プロキシ・IAM・TLSの既存条件を維持し、ConsumerへSQSを渡さず、HTTPクライアントは記事ごとに生成する。初期化失敗・利用終了・キャンセルで取得済み資源を解放し、通常の終了・診断障害で元の結果を変えない。

実装: 共通のarticle_fetch_lifecycleが設定検証とDB資源を管理し、補完compositionが実ConsumerとSQSを組み立てる。記事取得用EngineはNullPoolでHTTP待機中に物理接続を残さない。ReaderToolsはCrossref生成時にだけ全体設定を参照する。

Non-goals: イベント入口、可視性変更、抽出キャッシュ、旧Taskiqの接続、DB schema・権限、AWS設定・デプロイは変更しない。

Done: 先行する重要な起動・解放テスト、既存の単体・DB統合・ローカルテスト、lint・formatが成功し、一時環境を削除してスライス1を完了する。実装時は既存の未コミット仕様を保ったローカルmainで作業し、PR作成時に専用ブランチへ移す。

検証結果: 新規13ケースを含む単体6,988件、DB統合1,475件、ローカルテスト101件、Ruff lint・formatが成功した。一時環境の削除を確認した。既存Crossrefテストは設定の差し替え先だけを更新し、連絡先がHTTPヘッダーに渡る保証を維持した。スライス1を完了し、後続の抽出独立化・配送ハンドラー・AWS接続は未実装のままとする。

PR作成時の型整理: CompletionResourcesはBaseClientの代わりに、配送が必要とする可視性変更だけを定義したSqsMessageVisibilityClient Protocolを受け取る。SDKの生成・解放は変えず、独自のAWS継承階層・依存追加・型定義だけのテストは追加しない。
