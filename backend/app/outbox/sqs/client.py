"""SQSへの単一送信試行のクライアントを作る境界。"""

from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import NoRegionError
from botocore.session import Session


def create_sqs_client(*, session: Session, region: str) -> BaseClient:
    """SDKの内部再試行のないクライアントを作り、資格情報の取得はSDKのproviderに任せる。"""
    if not region:
        raise NoRegionError()
    return session.create_client(
        "sqs",
        region_name=region,
        config=Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )
