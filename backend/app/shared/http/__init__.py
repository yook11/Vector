"""外向き HTTP client の構築口を束ねる package。

client を分ける軸は「SSRF 対策をするか」ではなく **宛先が自分たちの管理下にあるか**。
SSRF guard も proxy 経路も、その分類から導かれる実装の詳細であって定義ではない。

submodule:
- ``external`` — 第三者のサービス、および記事本文のようにデータ由来の URL 宛。
  宛先を信用できないので SSRF 検証と DNS pin を通し、egress proxy を経由する。
- ``internal`` — 自 AWS アカウントに作った resource や自 deployment のコンテナ宛。
  宛先は設定層が確定させるので検証せず、egress proxy も経由しない。

``httpx.AsyncClient`` をこの 2 つ以外の場所で構築しない (``pyproject.toml`` の
``TID251`` で禁止する)。片方を選ぶことが宛先の分類を宣言することになる。

re-export はしない。利用側は submodule をフルパスで import する
(``from app.shared.http.internal import make_internal_async_client`` 等)。
``import httpx`` は絶対 import のため、この package が標準ライブラリの ``http`` を
shadow することはない。
"""

from __future__ import annotations
