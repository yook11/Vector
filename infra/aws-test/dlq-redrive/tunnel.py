"""検証環境を共通のSQSトンネルへ接続する。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "aws/scripts"))

from sqs_operations.aws_cli import AwsCli  # noqa: E402
from sqs_operations.tunnel import HOST, connect_proxy, sqs_tunnel as open_tunnel  # noqa: E402,F401


def sqs_tunnel(profile, fixture, credentials, aws):
    connection = fixture["private_connection"]
    return open_tunnel(
        AwsCli(credentials), connection["instance_id"], connection["document_name"]
    )
