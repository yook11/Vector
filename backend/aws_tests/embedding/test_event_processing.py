"""実SQSの初回配送と再配送を、完了記録と確定済みDB結果で確認する。"""
# ruff: noqa: S101

import json
import math


def require_valid_embedding(stored):
    assert stored is not None, "対象記事にベクトルが保存されていない"
    values = json.loads(stored)
    assert len(values) == 768, "保存されたベクトルの次元数が違う"
    assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
    assert any(v != 0 for v in values), "保存されたベクトルが全要素ゼロ"


def test_event_saves_embedding_for_target_article(aws_embedding):
    """未生成の記事のイベントをSQSへ投入すると、対象記事に有効なベクトルが確定保存される。"""
    event = aws_embedding.seed()
    assert aws_embedding.read(event) is None

    aws_embedding.deliver_and_wait(event, expected_reason="saved")

    require_valid_embedding(aws_embedding.read(event))


def test_redelivery_keeps_saved_embedding(aws_embedding):
    """同じイベントの再配送は生成済みとして正常終了し、初回に保存されたベクトルを変更しない。"""
    event = aws_embedding.seed()
    assert aws_embedding.read(event) is None
    first_message = aws_embedding.deliver_and_wait(event, expected_reason="saved")
    first_vector = aws_embedding.read(event)
    require_valid_embedding(first_vector)

    redelivered_message = aws_embedding.deliver_and_wait(
        event, expected_reason="already_embedded"
    )

    assert redelivered_message != first_message
    assert aws_embedding.read(event) == first_vector
