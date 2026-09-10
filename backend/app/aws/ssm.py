"""Parameter Storeの秘密情報を取得し、通信資源を取得操作内で閉じる。"""

import structlog
from botocore.config import Config
from botocore.session import Session
from pydantic import SecretStr

logger = structlog.get_logger(__name__)


def get_secret_parameter(*, region: str, path: str) -> SecretStr:
    """キャッシュせず、復号した値だけを秘密情報型で返す。"""
    client = Session().create_client(
        "ssm",
        region_name=region,
        config=Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 2},
            proxies={},
            ignore_configured_endpoint_urls=True,
        ),
    )
    try:
        response = client.get_parameter(Name=path, WithDecryption=True)
        parameter = response.get("Parameter")
        value = parameter.get("Value") if isinstance(parameter, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("SSM secret parameter must contain a non-empty string")
        return SecretStr(value)
    finally:
        try:
            client.close()
        except Exception as exc:
            try:
                logger.warning(
                    "ssm_parameter_cleanup_failed",
                    resource="ssm",
                    error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
                )
            except Exception:  # noqa: S110
                # 診断障害で取得結果や先行例外を置き換えない。
                pass
