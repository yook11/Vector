"""既存配送テストへ実際のEmbedding配送定義を接続する。"""

from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.route import EventDeliveryRoute
from app.outbox.publishing.routed_publisher import RoutedEventPublisher
from app.outbox.sqs.publisher import SqsSender


def make_embedding_publisher(*, embedding_queue_url, **kwargs):
    route = EventDeliveryRoute(
        "article.assessed_in_scope",
        embedding_queue_url,
        build_assessed_in_scope_message,
    )
    return RoutedEventPublisher(route, SqsSender(**kwargs))
