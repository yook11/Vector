"""ESA Djangoplicity 規格 RSS の per-source fetcher 群 (Phase 3 PR 3-b)。

ESA/Hubble + ESA/Webb は同型 (Djangoplicity News module) のため
``BaseDjangoplicityFetcher`` (`_common.py`) を共有し、subclass で ClassVar
(``NAME`` / ``ENDPOINT_URL`` / ``SITE_NAME``) のみ差し替える。

将来 ESO / ALMA など他の ESA Djangoplicity 系を追加する場合も同 base 上で
ClassVar 差し替えのみで済む。
"""
