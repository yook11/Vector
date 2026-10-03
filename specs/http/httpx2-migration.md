# HTTPX2への移行

更新日: 2026-10-03
状態: PR Aはマージ済み。PR BのSDK更新と契約試験は実装・ローカル検証済み。PR Cは未実施。各PR節に検証状況を記載する。
作業ブランチ: PR Bは `codex/gemini-sdk-upgrade`（PR Aマージ後のmainから分岐）

## Problem

Vectorの共通HTTP層を、圧縮応答の展開時に中間バッファを制限するHTTPX2へ移す。
Geminiと将来のTypeSafe接続にも同じ送信境界を使い、型を取り替えるためだけの中継層を増やさない。
旧HTTPXのパッケージが環境に存在することと、実際の外向き通信に使われることを区別する。
記事補完ではアプリが本文量を検査する前に圧縮応答が一括展開されるため、10MiBの本文上限だけでは中間メモリ消費を制限できない。
HTTPX2への移行でこの問題を抑える。全量読み取り経路の総量制限は別の残存課題として明示する。

## Evidence

### 移行理由と影響範囲

- HTTPXの最新公開版は0.28.1（2024-12-06）。2026-10-03時点で約22か月新規リリースがない。一方、公式リポジトリはarchived=false、pushed_at=2026-10-02であり、「保守停止」とは断定しない。HTTPX2側も旧HTTPXの活動が限定的と説明する。
- GHSA-8xx6-hgc6-gc2mの登録対象は `httpx2<2.12.0`。これを旧HTTPX向けのadvisoryとして引用しない。旧HTTPXへの影響は、0.28.1の `GZipDecoder.decode` が出力上限なしの `decompressor.decompress(data)` を呼ぶことと、下のローカル実測を根拠にする。旧HTTPXはContent-Encodingの段数も制限しない。
- 2026-10-03のOSV API照会では `PyPI/httpx@0.28.1` の該当結果は空、比較対象の `httpx2@2.11.0` には同GHSA/PYSEC-2026-3846が返った。現在のOSV照合ではこの問題を旧HTTPXについて検出できない。CIのSCAが緑でもこの経路の安全性を証明しない。CI自体の再実行は今回行っていない。
- 直接影響する経路は `backend/app/collection/article_completion/article_fetch.py` の記事本文・robots取得。`_read_body` は64KiBずつ本文を蓄積し、10MiBを超えるチャンクを追加する前に拒否する。Terraform上の補完Lambdaは `infra/aws/news_pipeline_completion.tf` で1024MB。同値が現在本番へ適用済みであることや、実LambdaでのOOMは今回検証していない。

### 採用版と互換性

- 移行開始時のlockは `google-genai==2.10.0` / `httpx==0.28.1` / `httpcore==1.0.9` / `h11==0.16.0` / `logfire==4.37.0` / `opentelemetry-instrumentation-httpx==0.62b1`。OTel SDK/API/exporterは1.41.1。
- `google-genai`は現在Googleが推奨するSDK系列であり、旧系列の`google-generativeai`への切り替えは行わない。
- 2026-10-03確認時の最新公開版 `google-genai==2.28.0` は、配布メタデータ上は引き続き `httpx>=0.28.1,<1.0.0` に依存する。
- ただし2.28.0の `HttpOptions.httpx_async_client` は `httpx.AsyncClient` と `httpx2.AsyncClient` の両方を受け取る。応答・ヘッダーの判定と一時的な通信例外の判定にもHTTPX2対応がある。
- 2.10.0の同フィールドは旧HTTPXだけを受け取る。2.28.0への更新とHTTPX2クライアントの明示的な注入を採用する。
- 配布wheelのSHA-256をPyPIの公開値と照合した。2.10.0: `d5350311567ae660c24cbc1752aee4b3d660f89c0106d2dcd2a69978c35afe1e`、2.28.0: `cdee15e2fbfea08ea61bce6e5bfbb3301db4bd13a52dccfa8c8d1167e37ca1cc`。
- `open_gemini_client` は既に共通HTTPクライアントをSDKへ渡し、要求ごとのtimeoutを接続設定で上書きし、SDKの試行数を1回に固定する。
- HTTPX2の圧縮応答の中間メモリ消費に対する修正は2.12.0以降。採用候補は確認に使った2.13.1。
- `logfire.instrument_httpx()` のHTTPX2対応はLogfire 4.39.0（2026-07-24、#2095）から。さらに `opentelemetry-instrumentation-httpx>=0.65b0` が必要。Logfire 4.39.0のhttpx extraは下限が0.42b0なので、Logfireの更新だけで必要版を保証しない。
- PR Aで `logfire[fastapi,httpx,sqlalchemy,system-metrics]>=4.39.0` と `opentelemetry-instrumentation-httpx>=0.65b0` を宣言した。Logfire 4.39.0、OTel instrumentation群・semantic-conventions・util-http 0.65b0、SDK/API/proto/exporter群1.44.0をlockした。FastAPI/ASGI/SQLAlchemy/system-metricsも同じinstrumentation版へ揃えている。

### 圧縮応答の限定したローカル実測

64MiBのゼロ列をgzip/二重gzipにし、模擬transportから最大64KiBずつ読み出した。ネットワーク送信や実Lambdaへの負荷試験は行っていない。

| 読み方 | HTTPX 0.28.1のピーク | HTTPX2 2.13.1のピーク |
| --- | --- | --- |
| レビュー添付のprobe（本文を保持せず、10MiB超で停止） | gzip/二重gzipとも192.1MiB | gzip 3.2MiB、二重gzip 3.3MiB |
| 現行 `_read_body` のASTを変更せず実行（本文をbytearrayへ蓄積） | gzip 192.10MiB、二重gzip 192.13MiB | gzip 14.11MiB、二重gzip 14.22MiB |

後者はドメイン用ラベルと上限例外だけを試験用定義に置き換え、実際の本文読み取りと上限判定を使った。再現データのgzipは65,250 bytes、二重gzipは269 bytes（mtime=0）。gzipヘッダー等で圧縮後の厳密な長さは変わる。
両ライブラリとも10MiB+64KiBを観測した時点で本文量を拒否した。したがって本文の10MiB制限は有効だが、旧HTTPXでは制限の判定前に大きな中間領域が作られる。
値は `tracemalloc` の追跡対象割り当てのピークで、事前生成した入力を含まず、プロセス全体のRSSではない。数KBで本番LambdaがOOMになるという定量的な断定には使わない。

再現コードと実測値: [article_read_memory_probe.py](/Users/yook1/.codex/visualizations/2026/10/02/01a0fc7d-4b69-7411-be8d-2cc3371c7f11/httpx2-migration-review/article_read_memory_probe.py)、[results.json](/Users/yook1/.codex/visualizations/2026/10/02/01a0fc7d-4b69-7411-be8d-2cc3371c7f11/httpx2-migration-review/results.json)。

### SDK単体のローカル互換性確認

Python 3.13.11 / google-genai 2.28.0 / httpx2 2.13.1 / pydantic 2.12.5の隔離環境で、実SDKとHTTPX2のMockTransportを接続した。
APIキーは合成値で、Gemini APIへは送信していない。

| ケース | 結果 |
| --- | --- |
| 通常JSON応答 | generate_contentからテキストを取得 |
| gzip応答 | 1回の展開でテキストを取得 |
| SSE応答 | generate_content_streamからテキストを取得 |
| HTTP 429 | SDKのClientError、code=429、HTTPX2応答とRetry-Afterを保持 |
| ReadTimeout | 同じHTTPX2例外オブジェクトを呼び出し側へ伝播 |

全ケースで送信1回、要求hookで指定したconnect/read/write/poolのtimeout、明示的なHTTPクライアントcloseを確認した。
これはSDK単体の確認であり、Vector全体の互換性・実proxy・TLS・本番動作の証明ではない。

再現用スクリプトと結果は作業成果物の `gemini-httpx2-compatibility/probe.py` / `results.json` に保存する。

## Decision

1. 旧HTTPXのまま計測依存を更新するPR A、旧HTTPXのまま `google-genai` を2.28.0へ更新するPR B、HTTPX2へ切り替えるPR Cに分ける。A/BをCより先に検証し、回帰原因を切り分ける。分割は互換性上の必須条件ではなく、変更を観測・切り戻しできる単位にするための判断。
2. 共通HTTP層とアプリ通信のRequest/Response/Timeout/例外をHTTPX2へ揃える。
3. `open_gemini_client`から、共通ファクトリが作るHTTPX2クライアントを既存の `httpx_async_client=` へ渡す。
4. SDKの依存として旧HTTPXは残す。全プロセスのimport差し替えやSDKのforkは導入しない。
5. ASGI/TestClient用の旧HTTPXは残せる。Gemini SDKも未注入の同期clientを旧HTTPXで内部生成するが、アプリは非同期clientだけを使う。appの静的な旧HTTPX importをlintで禁止し、実SDKの非同期呼び出しが注入したHTTPX2 clientを使う契約試験で送信経路を保証する。
6. TypeSafe側は後続タスクでHTTPX2 clientを直接注入する。型の中継は不要になるが、SDK 0.7.2による `httpx2.RequestError` の捕捉・再分類・causeの再生成は発生する。`LocalProtocolError` がConnectionErrorへ包まれること、DNS/OSErrorのerrnoやrequest属性が失われることを改めて変換仕様・契約試験で扱う。直接注入だけでは元の例外分類・属性の保持を保証しない。

## Invariants

- 外向き通信は `make_external_async_client` を通す。proxy必須、直接接続へのfallbackなし、呼び出し側のmounts/transport差し替え禁止を維持する。
- 送信前にHTTP/HTTPS限定とDNS/IP検査を適用する。redirect先の検査、元hostのHost/SNI、proxy時に元hostnameを保持する契約を維持する。
- 内部通信の `trust_env=False` とredirect禁止を維持する。
- HTTP段階のtimeout、記事取得全体のdeadline、Geminiの試行数1回を別の契約として維持する。
- 送信前拒否・接続・送信・受信・応答エラーの分類、原因チェーン、工程側の再試行方針を変えない。
- 記事補完の `_read_body` にある10MiBの本文制限を維持する。取得経路すべてに総量制限があるという前提は置かない。HTTPX2の中間バッファ制限は、本文全体の量を制限しない。
- 正常・失敗・キャンセル時のクライアント/streamの資源解放を維持する。
- 計測でheaders、request body、response bodyを取得しない。HTTPX2への変更後も外向き通信のspanが得られることを実測する。
- 証明書検証を無効化しない。HTTPX2の既定がOS trust storeへ変わるため、ECSと同じbackendイメージを使うLambda（取得・補完・評価等）の両方で、CA設定・TLS・Logfireのspan・対象処理の成功率を確認する。

## Non-goals

DB/schema、認証・認可、API応答、Terraform/egress設定、Jevによるルーティング、本番デプロイ、AIプロバイダーエラーの再分類は変更しない。
SDKが使わない同期クライアントやASGIテストから旧HTTPXの存在を完全に消すことを完了条件にしない。

### 総量制限がない経路の残存課題

`raw_http_client.py:46` のsitemap/HTML一覧取得は本文を全量保持する。実際には共通の `get_source_response` が `await client.get(...)` を行った時点で読み込みが完了しており、`.content` を参照する前に総量が割り当てられる。
同じ取得口を使う `rss_reader.py`、`algolia_hn_reader.py`、`crossref_reader.py` にも応答本文の総量制限がない。取得対象の制御はそれぞれ異なるが、HTTPX2への変更だけでは総量によるメモリ枯渇を防げない。
これらの本文上限・超過時のドメイン動作の新設は本移行PRの範囲外とし、別の是正作業として追跡する。本移行完了を「全取得経路のOOM対策完了」とは扱わない。

## 実装範囲と順序

### PR A: 計測依存を更新（旧HTTPXを維持）

- Problem: HTTPX2へ切り替える前に対応版の計測依存を導入し、現在のHTTPX通信・trace・機密情報の保護を維持する。
- Evidence: 公式の最低対応版、lock、`setup_logfire`、共通HTTP factory、既存の例外マスキング試験を確認した。
- Invariants: 公開API、設定項目、送信経路、例外分類、headers/bodyの非記録を維持する。
- Non-goals: Gemini SDK更新、HTTPX2導入、インフラ変更、本番デプロイは行わない。
- Done: lockが整合し、実spanの契約と既存試験、PRのセキュリティチェックが通ること。

#### 実装内容

- バージョン更新はLogfire/OTelの14パッケージだけ。`google-genai==2.10.0` / `httpx==0.28.1` / `httpcore==1.0.9`のlock項目は変更していない。HTTPX2関連の依存は追加していない。
- OTel APIが`importlib-metadata`へ依存しなくなったため、不要になった同パッケージとその依存`zipp`をlockから除去した。その他の既存パッケージのバージョンは維持する。
- 本番の初期化・redactor・HTTP処理は変更していない。追加試験は`backend/tests/logfire/test_httpx_instrumentation.py`が所有する。
- 実際の`setup_logfire`と`make_external_async_client`を実行し、export先だけをメモリに限定する。DNSの返答とhttpcoreのproxy送受信を模擬し、HTTPXのinstrumentationと共通transportは実行する。
- 通常span・親子関係・要求ヘッダー・応答ヘッダー・要求本文・応答本文・streamの遅延読取・streamのclose・timeout伝播・失敗span・例外自由文のマスキングを独立した11件で検証する。span数ではLogfireの開始通知用`pending_span`を除き、情報漏洩の検査には全spanを含める。
- 各試験の終了時にHTTPXの計測を解除し、メモリexporterの登録を外してstructlog設定を戻す。

#### 検証状況（2026-10-03）

- 更新前（Logfire 4.37.0 / OTel HTTPX 0.62b1）: 追加11件が通過。
- 更新後のLogfire・HTTP・Gemini関連: 494件が通過。Starletteの旧HTTPXに関する非推奨警告が1件あり、PR Aでは旧HTTPXを維持する。
- lint / format: appと追加試験を対象に通過。`uv lock --check`と`uv pip check`も通過。
- OSV APIでbackend lockの公開118パッケージを照会し、既知の該当advisoryは0件。PRのCI結果とは別のローカル照会であり、未公開の脆弱性がないことの保証ではない。
- 全体unit: `uv run --frozen --no-env-file pytest tests/ -m 'not integration' -x -q`で6,596件が通過（integration 1,209件を除外）。
- DB integration: `COMPOSE_DISABLE_ENV_FILE=true UV_FROZEN=true make test-integration PYTEST_ARGS='-x -q'`で1,209件が通過。専用PostgreSQL/Redisを使用し、終了時に回収する。
- PR CIの結果は該当PRのchecksに記録する。本番への反映・実通信での観測は未実施。

### PR B: Gemini SDKを更新（旧HTTPXを維持）

#### Work Definition

- Problem: HTTPX2を注入できるSDKへ更新し、HTTPライブラリ変更とSDK変更の回帰を切り分ける。
- Evidence: 公式配布物の依存・例外実装、共通client factory、runtime、embedding、既存の分類器と計測試験を照合する。
- Invariants: 公開API・設定・model・prompt・出力形式・例外分類・timeout・SDK試行数1回を維持し、利用範囲を抜けるとHTTP接続を解放する。
- Non-goals: HTTPX2導入、TypeSafe接続、ログ変換の再設計、stream途中終了直後の応答解放の修正、インフラ変更、本番デプロイ。
- Done: 依存更新、実SDKの契約試験、全体unit・integration、PRのCIとセキュリティチェックが通ること。

#### 実装内容

- `google-genai>=2.28.0,<3`を宣言し、lockでは`google-genai==2.28.0`と`google-auth==2.56.0`を採用する。SDKが`google-auth[requests]>=2.56.0,<3`を要求するため、既存の推移依存だけを更新する。
- パッケージの追加・削除はなく、バージョン変更はこの2件のみ。旧HTTPX 0.28.1、httpcore 1.0.9、PR AのLogfire・OTelと他の既存依存は維持する。lockのPython 3.14境界のresolution markerはresolverが生成したもの。
- 本番コードは変更しない。`error_translator.py`の旧HTTPX応答判定、Retry-Afterの保持、AIProvider分類を既存のまま検証する。
- `backend/tests/ai_providers/gemini/test_sdk_contract.py`に実SDKの16件を追加する。通常生成・構造化出力・embedding・SSE・HTTPエラー・通信例外を独立したケースとして確認する。
- 通常の契約試験はDNSとhttpcoreのproxy送受信を模擬し、`open_gemini_client`、共通transport、SDKの変換を実行する。注入したclient以外のHTTPX送信と同期送信は拒否する。
- 途中終了の接続解放は、本番compositionのruntimeを使い、DNSとTCP/TLSの入出力だけを模擬する。実際のproxy CONNECT・HTTP/1.1・接続プールを通して、利用範囲を抜けたときネットワークstreamのcloseが呼ばれることを確認する。実ネットワーク・TLS認証・本番環境の検証ではない。
- SDKクライアントのclose失敗で元の結果を変えないこと、初期化途中の失敗時の回収、計測とログの保護は既存試験を再利用する。

#### 例外の観測と残存課題

| 応答・失敗 | SDK 2.10.0 / 2.28.0で確認する契約 |
| --- | --- |
| HTTP 429 | `ClientError`が旧HTTPXのResponseとRetry-Afterを保持し、流量制限として分類される |
| 日次quotaのHTTP 429 | 構造化quotaIdが保持され、利用枠の枯渇として分類される |
| 非JSONのHTTP 503 | `ServerError`となり、サーバー失敗として分類される |
| HTTP 200の不正JSON | `JSONDecodeError`が未分類のまま伝播する |
| 不正JSONを含むSSE | `UnknownApiResponseError`が`JSONDecodeError`を原因として保持し、未分類のまま伝播する |
| SSE内のAPI code 429 | SDK例外のAPI codeは429、応答のHTTP statusは200として別々に観測できる |
| ReadTimeout / ConnectError | 元の例外オブジェクトと原因をSDKが保持し、再試行しない |

- 現行変換器はSSE内のAPI codeをHTTP statusとして扱う場合があり、この区別の修正は本PRに含めない。検討中の`specs/observability/gemini-sdk-exception-conversion-examples.md`も変更せず、2.10.0の観測と今回の2.28.0の証拠を区別する。
- streamを途中で`aclose()`した直後のHTTP応答解放は、両SDKで成立しなかった。これは更新前からの制約であり、2026-10-03の合意により修正は別課題とし、PR Bでは利用範囲終了時の接続解放を完了条件とする。
- 再現するには契約試験の`exchange`で未完了・完了のSSEを順に返し、runtimeから1つ目だけ受信して`await stream.aclose()`する。clientの利用範囲内で`assert exchange.body.closed`を行うと失敗する。応答bodyのcloseを2秒待っても完了しなかった。試験内でGCを強制したり、SDK内部を修正したりはしていない。
- 利用範囲終了時の接続解放試験は途中終了直後の応答解放を保証せず、SDKの応答オブジェクトがいつ回収されるかや本番の継続的なメモリ増加も未確認。

#### 検証状況（2026-10-03）

- SDK 2.10.0 / google-auth 2.49.2とSDK 2.28.0 / google-auth 2.56.0の両方で、最終版の追加契約試験16件が通過。両環境ともHTTPX 0.28.1 / httpcore 1.0.9を使用する。
- `uv lock --check` / `uv pip check`、appと追加試験のlint / formatが通過。lockのパッケージ比較でも更新はgoogle-genaiとgoogle-authの2件だけ。
- 全体unit: `uv run --frozen --no-env-file pytest tests/ -m 'not integration' -x -q`で6,619件が通過（integration 1,209件を除外）。Gemini client・分類器・runtime・embedding・Logfire・ログ保護の既存試験を含む。
- DB integration: `COMPOSE_DISABLE_ENV_FILE=true UV_FROZEN=true UV_NO_ENV_FILE=true make test-integration PYTEST_ARGS='-x -q'`で1,209件が通過。専用PostgreSQL/Redisとnetworkの回収も完了。
- Starletteの旧HTTPX、feedparserの互換マッピング、テスト内のLogfire未設定・伝播contextに関する警告は残る。機能やマスキングを無効化して回避していない。
- PR CI・セキュリティチェックの結果は当該PRのchecksへ記録し、上のローカル検証とは区別する。
- Gemini実API呼出し・デプロイ・本番観測は未実施。

### PR C: 共通HTTPと利用側をHTTPX2へ切り替える

- `backend/pyproject.toml` / `backend/uv.lock`：HTTPX2を追加し、アプリの直接依存を更新する。旧HTTPXはSDK/テスト用に残る。
- `backend/app/http/{external,internal,failure,error_mapping}.py`：型・transport・例外判定をHTTPX2へ揃える。
- `backend/app/ai_providers/gemini/{client,error_translator}.py`：HTTPX2 client注入と応答型の判定を同時に移す。
- collectionのreader、raw/source HTTP、article_fetch、AgentCore通信、関連scriptとmockテストを同じ型へ移す。ASGIテストの旧HTTPXは機械的に置換しない。
- `flake8-tidy-imports.banned-api` でappの旧 `httpx` モジュール参照を禁止する。HTTPX2の直接 `AsyncClient` 構築禁止も維持する。既存factoryのTID251ファイル全体免除を狭め、旧HTTPX禁止まで解除しないようにする。許可はHTTPX2の正規の構築・型参照に必要な行へ限定する。lint違反の小さな入力で、旧HTTPXのimport/from importと正規factoryの検出条件を確かめる。
- `backend/tests/ai_providers/gemini/test_client.py`と`test_sdk_contract.py`の実SDK試験を再利用する。旧HTTPX送信を失敗させた状態でも、生成・embedding・SSEが注入HTTPX2 transportを通ること、送信1回・timeout・429のRetry-After・元の通信例外・キャンセル・closeを独立したケースで検証する。
- HTTPX2導入後の実span生成を再確認する。PR Aで旧HTTPXのspanが出ることだけでは、PR Cの計測を証明しない。
- 圧縮応答は上限超過後に受信し続けず、本文保持量と展開中間バッファの両方が制限されることを、OOMを起こさない小さな入力で試験する。

### 送信境界と実行環境の検証

- HTTP/HTTPS proxy、宛先拒否、proxy停止時の直通fallbackなし、Host/SNI、証明書検証を実接続で確認する。
- httpcore2 2.13.1は直接接続で `sni_hostname` を使う。proxy向けCONNECTからはこの指定を除き、トンネル確立後の宛先TLSには使う。旧httpcore 1.0.9はCONNECTへextensionsをそのまま渡し、宛先TLSには元のorigin.hostを使うため、細部まで同じ実装ではない。現行の「proxy時は元hostnameのまま渡す」方針は維持し、移行を理由にDNS/IP方針を変更しない。
- 各PRで `/check` に従いlint・format・unit・DB integrationを実行する。未実行や環境制約は結果と分けて残す。
- デプロイ自体は本作業の範囲外。反映時はECS/LambdaでTLS、Logfire span、取得・補完・評価の成功率を確認し、ローカル試験と別の証拠として残す。

## Done

Vectorの対象アプリ通信とGeminiの実際の送信が共通HTTPX2経路を使い、上の契約を既存試験と必要な追加試験で確認できること。
SDK単体の成功だけで移行完了とはしない。実装、ローカル試験、CI、デプロイ、本番観測は別々に報告する。

## 一次情報

- [Gemini公式SDK一覧](https://ai.google.dev/gemini-api/docs/libraries)
- [google-genai 2.28.0の配布物](https://pypi.org/project/google-genai/2.28.0/)
- [2.28.0のHTTPクライアント型](https://github.com/googleapis/python-genai/blob/v2.28.0/google/genai/types.py)
- [2.28.0のHTTP応答・例外対応](https://github.com/googleapis/python-genai/blob/v2.28.0/google/genai/_api_client.py)
- [HTTPX2移行ガイド](https://pydantic.dev/docs/httpx2/get-started/migration/)
- [圧縮応答の中間メモリ消費に対する修正](https://github.com/pydantic/httpx2/security/advisories/GHSA-8xx6-hgc6-gc2m)

- [HTTPXの公開リリース](https://pypi.org/project/httpx/)
- [HTTPX2の保守方針](https://pypi.org/project/httpx2/)
- [OSV照会API](https://google.github.io/osv.dev/post-v1-query/)
- [Logfire 4.39.0リリース](https://github.com/pydantic/logfire/releases/tag/v4.39.0)
- [LogfireのHTTPX2計測と最低OTel版](https://github.com/pydantic/logfire/blob/v4.39.0/logfire/_internal/integrations/httpx.py)
- [Logfireの依存制約](https://github.com/pydantic/logfire/blob/v4.39.0/pyproject.toml)
- [OTel HTTPX 0.65b0の依存制約](https://github.com/open-telemetry/opentelemetry-python-contrib/blob/v0.65b0/instrumentation/opentelemetry-instrumentation-httpx/pyproject.toml)
