"""隔離ネットワーク内からHTTP・CONNECT・TLSを実際に送る試験用クライアント。"""

import http.client
import json
import ssl
import sys
from urllib.parse import urlsplit


def get(url: str, proxy: str | None) -> dict:
    destination = urlsplit(url)
    host = proxy or destination.hostname
    port = (
        3128
        if proxy
        else destination.port or (443 if destination.scheme == "https" else 80)
    )
    if destination.scheme == "https":
        context = ssl.create_default_context(cafile="/tls/cert.pem")
        # Python 3.13のcreate_default_contextで証明書とホスト名を検証する。
        # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected  # noqa: E501
        connection = http.client.HTTPSConnection(host, port, timeout=5, context=context)
        if proxy:
            connection.set_tunnel(destination.hostname, destination.port or 443)
        target = destination.path or "/"
    else:
        connection = http.client.HTTPConnection(host, port, timeout=5)
        target = url if proxy else destination.path or "/"
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        return {"status": response.status, "body": response.read().decode()}
    finally:
        connection.close()


def connect(authority: str, proxy: str) -> dict:
    connection = http.client.HTTPConnection(proxy, 3128, timeout=5)
    try:
        connection.request("CONNECT", authority)
        response = connection.getresponse()
        # 成功時はHTTP本文ではなくトンネルになるため、拒否応答の本文だけを読む。
        body = response.read().decode() if response.status != 200 else ""
        return {"status": response.status, "body": body}
    finally:
        connection.close()


if __name__ == "__main__":
    request = json.load(sys.stdin)
    if request["method"] == "CONNECT":
        result = connect(request["authority"], request["proxy"])
    else:
        result = get(request["url"], request["proxy"])
    print(json.dumps(result))
