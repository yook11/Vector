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

## 送信経路

通常の外部通信は次の順序で進む。

1. 利用機能が`make_external_async_client`を生成する。
2. `HttpSettings`が必須のプロキシ設定を検証する。
3. 標準transportが送信直前に、IP直書きまたはDNS解決結果の公開性を検証する。
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
- `destination_resolution.HostResolutionError`は、名前解決自体の失敗を表す。
- 標準transportはこれらを元のまま伝播させる。HTTPの失敗分類はDNS失敗を通信失敗として扱い、宛先拒否を混ぜない。
- 例外名・メッセージ・原因連鎖は維持し、例外の完全修飾名は新しい所属モジュールに変わる。
- `SafeUrl`は形式・文字数とIP直書きの公開性を検証する既存の型であり、DNS検証や接続先の保証を持たない。

## テストによる確認範囲

| テスト | 所有する保証 |
|---|---|
| `test_http/test_destination_policy.py` | IPv4/IPv6の解析・公開性・不変性、JSONの非公開レンジとIPv4-mapped表記の拒否 |
| `test_http/test_destination_resolution.py` | 公開IPの返却、公開・非公開IPの混在拒否、DNS失敗と原因連鎖 |
| `test_http/test_external_http.py` | 送信前の拒否、直接接続のIP固定・Host/SNI保持、プロキシ経路、リダイレクト先検証、非公開IP拒否ACLの評価順序 |
| `test_http/test_settings.py` | 必須のプロキシ設定と、呼び出し側からの経路上書きの拒否 |
| `test_http/test_failure.py`・`test_http/test_error_mapping.py` | DNS失敗の分類と、宛先拒否を通信失敗に混ぜないこと |
| `collection/test_external_fetch_error_mapping.py` | 利用機能での宛先拒否・通信失敗の意味づけ |
| `test_shared/test_safe_url.py` | SafeUrlの既存契約 |
| AWS試験用`test_snapshot.py` | JSONの同梱・内容保持と、保存されたTerraformからの相対参照 |

HTTPの単体テストはDNSと実送信をモックする。
ACLの静的検証やTerraformのmock testは、実環境のSquidによる接続拒否の実測とは区別する。

## 後続で解消する保証不足

目標は、対象となるすべての外部通信で、宛先方針と検証済み経路を必ず使用することである。
今回の責務・配置整理では、次の既存動作を変更していない。

- ファクトリは`mounts`をHTTPXへ委譲でき、別transportを選ぶ経路は標準transportの検証を通らない。
- 送信境界にはHTTP/HTTPS限定の独自検証がなく、HTTPX/httpcoreに委ねている。
- 共通クライアントを注入せずSDKを直接構築するAgent、briefing、workerのembedding等が残る。
- SafeUrlのパーサー間の解釈差や、形式検証と公開IP判定の責務分離は後続で扱う。

したがって、標準transportの保証を、任意のファクトリ設定や全SDK通信の保証として扱わない。
