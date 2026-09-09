"""資格情報取得とSQSへの単一送信試行の境界。"""

from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import (
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)
from botocore.session import Session

from app.outbox.publishing.errors import (
    PublishError,
    PublishPhase,
)
from app.outbox.sqs.error_mapping import publish_error_from_exception


def create_sqs_client(*, session: Session, region: str) -> BaseClient:
    """試行用の資格情報を確定してから、内部再試行のないクライアントを作る。"""
    phase = PublishPhase.INITIALIZE
    try:
        if not region:
            raise NoRegionError()
        phase = PublishPhase.RESOLVE_CREDENTIALS
        credentials = session.get_credentials()
        if credentials is None:
            raise NoCredentialsError()
        frozen = credentials.get_frozen_credentials()
        if not frozen.access_key or not frozen.secret_key:
            raise PartialCredentialsError(provider="publisher", cred_var="credentials")
        phase = PublishPhase.INITIALIZE
        return session.create_client(
            "sqs",
            region_name=region,
            aws_access_key_id=frozen.access_key,
            aws_secret_access_key=frozen.secret_key,
            aws_session_token=frozen.token,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
    except PublishError:
        raise
    except Exception as exc:
        raise publish_error_from_exception(exc, phase=phase) from exc
