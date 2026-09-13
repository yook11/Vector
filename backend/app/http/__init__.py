"""HTTPの宛先方針・送信経路・通信失敗の定義を束ねるパッケージ。

client を分ける軸は「SSRF 対策をするか」ではなく **宛先が自分たちの管理下にあるか**。
SSRF guard も proxy 経路も、その分類から導かれる実装の詳細であって定義ではない。

submodule:
- ``destination_policy`` — 外部通信の最終宛先に許可するIPの共通方針。
- ``destination_resolution`` — DNS解決と、その結果への共通方針の適用。
- ``failure`` — 単一の通信試行の失敗種別とリクエスト到達可能性。
- ``external`` — 第三者のサービス、および記事本文のようにデータ由来の URL 宛。
  標準transportが送信時に宛先を検証し、設定で確定したegress proxyへ渡す。
- ``internal`` — 自 AWS アカウントに作った resource や自 deployment のコンテナ宛。
  宛先は設定層が確定させるので検証せず、egress proxy も経由しない。

``httpx.AsyncClient`` をこの 2 つ以外の場所で構築しない (``pyproject.toml`` の
``TID251`` で禁止する)。片方を選ぶことが宛先の分類を宣言することになる。
処理ごとのドメイン制限はプロキシ設定が持ち、保証範囲と未統一の経路はREADMEに記す。

re-export はしない。利用側は submodule をフルパスで import する
(``from app.http.internal import make_internal_async_client`` 等)。
``import httpx`` は絶対 import のため、この package が標準ライブラリの ``http`` を
shadow することはない。
"""

from __future__ import annotations
