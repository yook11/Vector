"""外部・内部の通信で、接続相手の証明書を検証する基準を定める。"""

import ssl

import certifi


def create_verification_context() -> ssl.SSLContext:
    """certifiのルート証明書一覧で接続相手を検証するcontextを作る。

    httpx2の既定はOSの証明書ストアだが、実行環境で一覧が変わらないようcertifiに固定する。
    """
    return ssl.create_default_context(cafile=certifi.where())
