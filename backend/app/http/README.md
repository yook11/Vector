# HTTPの宛先方針と適用責任

外部通信は、第三者のAPIやデータ由来の記事URLなど、自分たちの管理下にない宛先への通信を指す。
自AWSリソースや自deploymentへの内部通信は、設定で宛先を限定して`internal`を使う。
この分類はIPの公私とは異なり、内部プロキシへの接続も最終的な外部宛先とは分けて扱う。

## 方針の所有者と適用箇所

| 担当 | 責任 | 保証の範囲 |
|---|---|---|
| `destination_policy.py` | 外部通信の最終宛先に許可するIPの共通方針 | `PublicIpAddress`で解析と公開性を検証する。DNS解決や送信は行わない |
| `destination_resolution.py` | DNS解決と、その結果への共通方針の適用 | 解決結果に非公開IPが含まれれば拒否する。実際の接続先は保証しない |
| `external.py` | 標準transportによる送信時の適用 | IP直書きを再検証し、DNS名には解決・検証を適用する。呼び出し側の事前検証に依存しない |
| `settings.py` | 外部クライアントのプロキシ経路の確定 | 必須のプロキシURLを内部namespaceに限定する。設定不備はtransport生成前に拒否する |
| プロキシ | 自身が解決した接続先の検査と、実行単位ごとの制限 | 非公開IP・ポート・許可ドメインをACLで制限する |
| 利用機能 | 失敗の意味づけ | 宛先拒否やDNS失敗を、記事取得失敗などへ変換する |

処理ごとの許可ドメインは、[実行単位の設定](../../../infra/aws/locals.tf)と
[プロキシ設定](../../../infra/aws/proxy.tf)が持つ。
workerやLambdaの送信元に対応するACLを使い、記事取得は可変のサイトを許可し、
API呼び出しは実行単位に必要なベンダードメインへ限定する。
アプリ側で処理別ポリシーを選択・差し替えする仕組みは持たない。

## 通信の種類と実行単位の設定

固定のAPI宛先も可変の記事URLも、共通HTTPの標準経路ではIP検証後にプロキシを通る。
両者の違いは経路ではなく、送信元の実行単位に適用するドメイン制限である。

| 通信の種類 | 実行単位の例 | アプリ側 | プロキシ側 |
|---|---|---|---|
| 許可ドメイン限定の外部通信 | `analysis`、`assessment_consumer`、`embedding_consumer`、`curation_consumer` | 送信前のIP検証・プロキシ必須 | 許可ドメイン・非公開IP・ポートの制限 |
| 可変ドメインへの外部通信 | `fetch`、`acquisition_consumer`、`completion_consumer` | 送信前のIP検証・プロキシ必須 | ドメインを限定せず、非公開IP・ポートを制限 |

[実行単位の設定](../../../infra/aws/locals.tf)の`egress_vendors`は、
[プロキシ設定](../../../infra/aws/proxy.tf)のベンダードメイン一覧へ対応する。
`egress_allow_any_domain`はプロキシへ渡す`allow_any_domain`に対応し、
consumer専用の設定も同じプロキシ設定内で定義する。
`allow_any_domain = true`でも非公開IP・許可外ポートの拒否は解除しない。
[Squidテンプレート](../../../infra/aws/templates/squid.conf.tftpl)はこれらの拒否をドメイン許可より先に評価する。

この識別は送信元CIDRによる実行単位ごとであり、同じ実行単位内のHTTPクライアントごとの権限分離ではない。
許可ドメインの正本はinfra側に置き、アプリ側に一覧や処理別ポリシー選択を追加しない。

## 送信経路

通常の外部通信は次の順序で進む。

1. 利用機能が`make_external_async_client`を生成する。
2. `HttpSettings`が必須のプロキシ設定を検証する。
3. 標準transportが送信直前に、IP直書きまたは`resolve_public_host_addresses`によるDNS解決結果の公開性を検証する。
4. 元のホスト名をプロキシへ渡す。
5. プロキシが自身の名前解決結果を検査し、ポート・送信元・許可ドメインの制限を適用する。

アプリとプロキシのDNS解決結果が同じとは限らないため、プロキシ側の非公開IP拒否が必要になる。
通常経路でアプリは接続先をIPへ書き換えず、アプリで検証したIPへの接続を保証するわけではない。
httpcoreのCONNECTトンネルは`sni_hostname`を引き継がないため、IPへの書換えはTLSのホスト名検証を壊す。

既存の`_PinnedDnsTransport`をプロキシなしで構築した場合は、検証した最初のIPへ接続先を固定し、
HostヘッダーとTLS SNIを元のホスト名に保つ。
この直接接続動作は維持するが、ファクトリはプロキシ必須であり、直接接続へのfallbackはない。

リダイレクトの既定は`follow_redirects=False`。
明示的に追従させた場合、標準transportを通る各リクエストの宛先に同じ検証を適用する。
追従するかどうかと、追従先への宛先方針の適用は別の責任である。

## IPレンジと例外の契約

[non_public_ranges.json](non_public_ranges.json)は非公開IPレンジの正本で、
アプリと、本体・AWS試験用Terraformが参照する。
[Squidテンプレート](../../../infra/aws/templates/squid.conf.tftpl)はこのレンジからACLを生成する。
AWS試験のスナップショットにも、同じ相対配置でJSONを同梱する。

アプリはJSONのレンジに加えてPythonの`ipaddress`判定を使う。
同じIPについて「プロキシの非公開レンジ拒否 ⊆ アプリの拒否」を維持し、拒否集合の完全一致は要求しない。
この関係はドメイン・ポート制限を含むプロキシの全拒否条件や、両者のDNS解決結果の一致を意味しない。

- `destination_policy.HostBlockedError`は、IP直書きまたはDNS解決結果の方針上の拒否を表す。
- `destination_resolution.HostResolutionError`は、名前解決の失敗・空の結果・IP形式でない結果を表す。
- 標準transportはこれらを元のまま伝播させる。HTTPの失敗分類はDNS失敗を通信失敗として扱い、宛先拒否を混ぜない。
- 例外名・メッセージ・原因連鎖は維持し、例外の完全修飾名は新しい所属モジュールに変わる。
- `SafeUrl`は形式・文字数とIP直書きの公開性を検証する既存の型であり、DNS検証や接続先の保証を持たない。

名前解決の成功条件は、結果が1件以上あり、全件が有効かつ許可されたIPであることとする。
成功時は解決結果の順序で`PublicIpAddress`の一覧を返す。
禁止IPが混在すれば全体を拒否し、IP形式でない結果も読み飛ばさない。
空・不正な結果を解決失敗へ変更しても、既存のDNS例外・宛先拒否のメッセージと原因連鎖は維持する。

## エラーの共通契約と変換責任

共通HTTPは確認できた発生事実を保持し、終了・再試行・待機の判断は利用機能が所有する。
通信ライブラリの例外解析は`failure.classify_httpx`へ集約し、
`error_mapping`はその結果を`HttpTransportError`へ載せる。
記事の取得工程・補完工程はどちらも共通HTTPエラーを受け取り、
例外メッセージから独自にstatusを読み取らない。

| 発生事実 | HTTP側の扱い | 保証しないこと |
|---|---|---|
| IP形式不正・非公開IP | `NotAnIpAddressError`・`NotAPublicIpError`はIP解析・判定の内部契約 | DNS名をIPとして解析できないことを、URL不正や通信失敗とはしない |
| アプリの宛先方針による拒否 | 送信境界・DNS検証が`HostBlockedError`で伝える | プロキシから拒否応答が返ったとはしない |
| 事前のDNS解決失敗・空または不正な解決結果 | `HostResolutionError`を`PREPARATION / DNS_RESOLUTION`へ分類する | 方針による拒否とはしない |
| 接続中のDNS・TLS・TCP障害 | 確認できた原因を`CONNECT`段階の理由へ分類する | 例外だけで接続相手がプロキシか取得先かを推測しない |
| プロキシ接続・CONNECT失敗 | `ProxyError`を`CONNECT / PROXY`とし、読めたstatusを`proxy_status`へ保持する | 403だけで拒否したACLや再試行可否を決めない |
| 通常のHTTP非成功応答 | `HttpResponseError`へstatus・受信時刻・生のRetry-Afterを保持する | 応答の生成元が取得先か中継プロキシかをstatusだけで断定しない |
| URL・リクエストの不正など分類対象外 | 通信失敗への変換は`None`を返し、新経路の呼び出し側が元の例外を伝播する | 不明な例外を一律に通信障害へ丸めない |

`http_transport_error_from_exception`は例外を返す関数であり、自動で送信処理全体を包むものではない。
呼び出し側が捕捉する範囲を決め、変換した例外を`raise mapped from exc`で伝えて原因連鎖を保持する。
`HostBlockedError`は通信障害の分類対象外として維持し、エラーを一つの共通基底へまとめるための移動は行わない。

### プロキシについて観測できる範囲

HTTPSのCONNECTが非2xxなら、導入済みHTTPX/httpcoreは`ProxyError`を返す。
例外に構造化statusがないため、共通分類器がメッセージ先頭の3桁を読み取り、読めなければ`None`を保持する。
HTTP転送では403も通常の応答として返り得るため、statusや`X-Squid-Error`等のヘッダーだけでプロキシ拒否へ変換しない。
CONNECT成功後に受け取った403も、CONNECTそのものの拒否とは区別する。

プロキシのTCP接続が失敗した場合は`ConnectError`になることがあり、必ず`ProxyError`になるとは限らない。
現在の分類器は経路情報を受け取らないため、この場合は`CONNECT / NETWORK_IO`など確認できた事実だけを保持する。
経路を識別する情報の追加や、通常応答の生成元を確定する仕組みは今回の共通化に含めない。
機構の違いは[HTTPXのプロキシ説明](https://www.python-httpx.org/advanced/proxies/)と
[例外定義](https://www.python-httpx.org/exceptions/)を参照する。

### 利用機能の既存判断

記事の取得工程と補完工程は、共通HTTPエラーと宛先拒否を[外部取得の失敗判断](../../../specs/collection/external-fetch-failure-classification.md)で再試行可能・再試行不可に分け、後始末は各工程が決める。
CONNECT 403を含むプロキシ失敗は通信失敗として再試行可能に分類し、プロキシ接続時の403だけで取得不可と扱わない。
取得工程にあった未知例外を通信障害へ倒す互換動作と、エラー自身の`retryable`は撤去した（[取得工程の共通HTTPエラーへの移行](../../../specs/collection/acquisition-common-http-errors.md)）。
失敗情報の内部保持と安全な記録の責任は[Issue #328](https://github.com/yook11/Vector/issues/328)と整合させる。

## テストによる確認範囲

| テスト | 所有する保証 |
|---|---|
| `test_http/test_public_ip_address.py` | IP単体の生成・形式の境界値・禁止レンジ両端の拒否・IPv4-mapped表記の拒否・表記の正規化と同一性・不変性 |
| `test_http/test_destination_resolution.py` | OSの解決結果からIPを取得、検証済みIPの順序付き返却、混在結果の全体拒否、空・不正な結果の失敗、原因連鎖 |
| `test_http/test_external_http.py` | DNS結果に基づく送信可否、直接接続のIP指定・Host/SNI保持、プロキシ経路、リダイレクト先検証 |
| `test_http/test_egress_proxy_config.py` | Squid設定テンプレートの非公開IP拒否ACL・レンジ参照・評価順序 |
| `test_http/test_settings.py` | 必須のプロキシ設定と、呼び出し側からの経路上書きの拒否 |
| `test_http/test_failure.py`・`test_http/test_error_mapping.py` | DNS失敗の分類と、宛先拒否を通信失敗に混ぜないこと |
| `test_http/test_proxy_failure.py` | HTTPX/httpcoreを通したCONNECT拒否・通常応答・TCP障害の区別（ネットワークとTLSはモック） |
| `test_shared/test_safe_url.py` | SafeUrlの既存契約 |
| AWS試験用`test_snapshot.py` | JSONの同梱・内容保持と、保存されたTerraformからの相対参照 |

DNSの単体テストはOSの名前解決またはその返答を差し替え、実DNSへ問い合わせない。
HTTPの振る舞いのテストはDNSの返答と実送信をモックし、アプリの宛先検証は実際に動かす。
拒否時に送信処理へ進まないことと、送信処理へ渡すRequestを検証し、実TCP接続やTLS認証の成立までは保証しない。
記事取得側はHTTPの例外を入力とし、IPごとの許可・拒否を繰り返さない。
ACLの静的検証やTerraformのmock testは、実環境のSquidによる接続拒否の実測とは区別する。

## 設定の検証とデプロイ後の確認

`HttpSettings`はプロキシURLを必須にし、接続先の内部namespaceを検証する。
この検証を通っても、プロキシのデプロイ・稼働・ACL適用を確認したことにはならない。
Terraformのvalidateやmock testも、デプロイ済み環境の動作を保証しない。

デプロイ後には、実行単位ごとにプロキシへの到達、許可先への疎通、禁止ドメイン・非公開IP・許可外ポートの拒否、
プロキシを迂回する外部への直接接続の拒否を確認する必要がある。
アプリとプロキシのDNS解決結果が異なる場合の宛先拒否も実動作で確認する対象とする。
この確認はデプロイ後の検証が担当し、アプリのクライアント生成時には追加しない。

## 後続で解消する保証不足

目標は、対象となるすべての外部通信で、宛先方針と検証済み経路を必ず使用することである。
今回の責務・配置整理では、次の既存動作を変更していない。

- ファクトリは`mounts`をHTTPXへ委譲でき、別transportを選ぶ経路は標準transportの検証を通らない。
- 送信境界にはHTTP/HTTPS限定の独自検証がなく、HTTPX/httpcoreに委ねている。
- 共通クライアントを注入せずSDKを直接構築するAgent、briefing、workerのembedding等が残る。
- SafeUrlのパーサー間の解釈差や、形式検証と公開IP判定の責務分離は後続で扱う。

したがって、標準transportの保証を、任意のファクトリ設定や全SDK通信の保証として扱わない。
