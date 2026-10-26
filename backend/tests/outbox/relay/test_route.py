"""配送先の対応が必須とする項目を、DB・通信を使わず生成時に検証する。"""

import pytest

from app.outbox.relay.route import EventDeliveryRoute

EVENT_TYPE = "article.assessed_in_scope"
QUEUE_URL = "https://sqs.invalid/embedding"


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("event_type", " ", ValueError),
        ("queue_url", "\t", ValueError),
        ("event_type", None, TypeError),
        ("queue_url", None, TypeError),
    ],
)
def test_route_requires_explicit_nonblank_fields(field, value, error):
    """配送先と種別を必須とし、未設定を既定値で補わない。"""
    args = dict(event_type=EVENT_TYPE, queue_url=QUEUE_URL)
    args[field] = value
    with pytest.raises(error):
        EventDeliveryRoute(**args)
