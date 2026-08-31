"""自分たちの resource 宛の ``httpx.AsyncClient`` のファクトリ。

宛先が自 AWS アカウントに作った resource や自 deployment のコンテナである経路 —
AgentCore Gateway、frontend の revalidate 口など — はここを通す。第三者宛には
``app.shared.http.external`` を使う。

**宛先が internal であることをこの関数は検証しない。** 根拠は設定層の起動時
バリデータ (``app/config.py`` の ``_validate_internal_frontend_base_url`` と
production narrowing) にあり、宛先はここへ渡る時点で確定している。名前が
保証しているように見えるが、保証しているのは設定層である。

内部宛の通信が egress proxy を経由することは定義上ありえないので、環境変数から
proxy 設定を拾わない (httpx の ``trust_env``)。``NO_PROXY`` に内部 host を
書き忘れると proxy へ迂回して静かに失敗するが、その正しさに依存させない。
``SSL_CERT_FILE`` や ``.netrc`` も同じ switch で無効になるが、どちらもこの
実行環境では設定していない。

``transport`` と ``proxy`` は受け取らない。httpx は transport を明示すると env の
proxy を読まなくなる (``allow_env_proxies = trust_env and transport is None``) ため、
渡せる口があること自体が経路を壊す罠になる。
"""

from __future__ import annotations

import httpx


def make_internal_async_client(*, timeout: float) -> httpx.AsyncClient:
    """egress proxy を経由しない ``httpx.AsyncClient`` を返す。

    ``follow_redirects`` は無効。内部サービスが外向きへ redirect した場合に、
    信頼境界を跨いだ先へ資格情報を送らないため。
    """
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )
