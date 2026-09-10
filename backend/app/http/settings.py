"""外部HTTP通信に必要な設定をアプリ全体から独立して読み込む。"""

from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 先頭のdotでホスト境界を固定し、通知先とプロキシで同じ内部namespaceを使う。
INTERNAL_HOST_SUFFIXES = (".flycast", ".vector.internal")
INTERNAL_NAMESPACE_GLOBS = " / ".join(f"*{suffix}" for suffix in INTERNAL_HOST_SUFFIXES)


class HttpSettings(BaseSettings):
    """環境変数から必須のプロキシ設定を取得する。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    egress_proxy_url: str

    @field_validator("egress_proxy_url")
    @classmethod
    def _validate_egress_proxy_url(cls, v: str) -> str:
        """外向き通信のプロキシを既存の内部namespaceに限定する。"""
        scheme = urlparse(v).scheme
        if scheme not in ("http", "https"):
            raise ValueError(
                f"EGRESS_PROXY_URL must use http or https scheme, got {scheme!r}"
            )
        host = urlparse(v).hostname
        if host is None:
            raise ValueError("EGRESS_PROXY_URL must include a host")
        if not host.endswith(INTERNAL_HOST_SUFFIXES):
            raise ValueError(
                f"EGRESS_PROXY_URL host {host!r} is not an internal namespace host "
                f"({INTERNAL_NAMESPACE_GLOBS})"
            )
        return v
