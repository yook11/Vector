"""実Curation relayのDB署名とSQS通信だけをローカルへ接続する。"""

from copy import deepcopy
from hashlib import md5
from types import SimpleNamespace
from unittest.mock import Mock

from app.outbox.sqs import publisher
from tests.iam_fixtures import inject_test_db_signer

CURATION_QUEUE_URL = (
    "https://sqs.ap-northeast-1.amazonaws.com/123456789012/article-curation"
)


def configure_curation_relay(monkeypatch, database_url):
    database_url = inject_test_db_signer(monkeypatch, database_url)
    for name, value in {
        "ENV": "test",
        "DATABASE_URL": database_url,
        "DB_IAM_AUTH": "true",
        "AWS_REGION": "ap-northeast-1",
        "SQS_ARTICLE_CURATION_QUEUE_URL": CURATION_QUEUE_URL,
    }.items():
        monkeypatch.setenv(name, value)
    state = SimpleNamespace(batches=[], error=None)

    def send(**request):
        state.batches.append(deepcopy(request))
        if state.error is not None:
            raise state.error
        return {
            "Successful": [
                {
                    "Id": entry["Id"],
                    "MessageId": f"sqs-{entry['Id']}",
                    "MD5OfMessageBody": md5(
                        entry["MessageBody"].encode(), usedforsecurity=False
                    ).hexdigest(),
                }
                for entry in request["Entries"]
            ]
        }

    def create_client(**kwargs):
        return Mock(send_message_batch=Mock(side_effect=send))

    monkeypatch.setattr(publisher, "create_sqs_client", create_client)
    return state
