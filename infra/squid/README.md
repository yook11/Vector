# Squidの実動作テスト

製品のDockerfile・entrypointと`infra/aws/templates/squid.conf.tftpl`を使い、
実Squidによる宛先・送信元・管理機能の許可・拒否を確認する。
非公開IPレンジは`backend/app/http/non_public_ranges.json`をそのまま使う。

## 実行

Docker、Terraform、OpenSSL（`req -addext`対応）、backendの開発依存（pytest）が必要。
試験用サーバーには既存のPythonイメージを使う。

```sh
# イメージ取得と依存の準備は試験開始前に行う。
docker pull python:3.13-alpine
uv sync --project backend --no-env-file --frozen

# リポジトリルートから実行する。
uv run --project backend --no-env-file --frozen pytest infra/squid/tests -q
```

Squidイメージは実行時に製品のDockerfileからビルドするため、初回はAlpineと
パッケージの取得が必要になる。試験中の通信は隔離ネットワーク内だけで行う。
環境不足やSquidの起動失敗はskipせず失敗させる。

## 試験環境と保証の範囲

- 試験サーバーは80・443・8080番で動き、ポートごとのTCP接続数と要求を管理用loopbackポートから観測する。
- クライアントはアプリの事前検証を通さず、HTTP要求・CONNECT要求・トンネル経由のHTTPS要求を送る。
- TLS証明書と鍵は実行時に一時ディレクトリへ生成し、クライアントは試験証明書だけを信頼してホスト名も検証する。
- 設定は空の一時ディレクトリでTerraformの`templatefile`を評価して生成し、実設定・state・`.env`は読まない。
- 公開IP相当の`93.184.216.0/24`と、非公開IPの`10.254.216.0/24`・`fd00:285::/64`を、外向き経路のないDocker内部ネットワークへ割り当てる。
- Squidと試験サーバーを両ネットワークへ接続し、禁止IPのACLやポート制限を緩めずに試験する。
- Docker内蔵DNSで公開側のドメインと非公開側の`private.test`を解決し、ホストファイルによる固定ではなくSquidの名前解決を通す。
- 拒否試験の事前到達確認はSquidと同じネットワーク名前空間で実行し、拒否後に宛先の接続数が増えないことを確認する。
- ポートをホストへ公開せず、DNSの外部転送先はloopbackへ限定する。同じサブネットを使うため並列実行しない。
- 各ケースのコンテナ・ネットワークは終了時に削除し、失敗時にはコンテナログを表示する。

送信元の試験データは、条件をコメントと照合できるよう固定する。

| 送信元IP | 許可条件 |
|---|---|
| `93.184.216.20`（A） | `allowed.test`・`private.test`だけを許可する |
| `93.184.216.21`（B） | `source-b.test`だけを許可する |
| `93.184.216.22`（記事取得） | ドメインを限定しない |
| `93.184.216.23`（未登録） | どの許可ルールにも含めない |

ドメインを許可しても、非公開IP・許可外ポート・管理機能は先に拒否する。
送信元AからB専用の宛先へ接続できないことと、B自身は接続できることを別ケースで確認する。
非公開IPの拒否はIPv4・IPv6について、HTTPとCONNECTの両経路で確認する。

アプリとのDNS結果の食い違い、AWS上の送信元と設定の対応、配置や直接外向き通信の禁止は後続で確認する。
アプリのHTTPエラー変換と、設定テンプレートの静的テストは引き続きbackend側が所有する。

参考: [Dockerの内部ネットワーク](https://docs.docker.com/reference/cli/docker/network/create/#network-internal-mode---internal)、
[Terraform console](https://developer.hashicorp.com/terraform/cli/commands/console)。
