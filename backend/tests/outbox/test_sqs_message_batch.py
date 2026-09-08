"""1回の送信単位としての件数・ID・本文サイズと不変性を検証する。"""

from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest

from app.outbox.sqs_message import MAX_MESSAGE_BYTES, SqsMessage
from app.outbox.sqs_message_batch import SqsMessageBatch


@pytest.mark.parametrize("count", [1, 10])
def test_batch_preserves_message_order_and_contents(count):
    """構築済みの本文とMD5を変更せず、渡された順序で保持する。"""
    messages = tuple(
        SqsMessage(event_id=UUID(int=count - index), body=f'{{"value": {index}}}')
        for index in range(count)
    )
    before = [
        (message.event_id, message.body, message.body_md5) for message in messages
    ]

    batch = SqsMessageBatch(messages=messages)

    assert len(batch.messages) == count
    assert all(
        actual is original
        for actual, original in zip(batch.messages, messages, strict=True)
    )
    assert [
        (message.event_id, message.body, message.body_md5) for message in batch.messages
    ] == before


@pytest.mark.parametrize("count", [0, 11])
def test_batch_rejects_empty_or_too_many_messages(count):
    """空の送信や1リクエストの件数上限超過を構築時に拒否する。"""
    messages = tuple(SqsMessage(UUID(int=index), "{}") for index in range(count))
    with pytest.raises(ValueError):
        SqsMessageBatch(messages=messages)


@pytest.mark.parametrize(
    "messages", [None, [], "private", (object(),), (object(),) * 11]
)
def test_batch_rejects_container_and_element_type_before_count(messages):
    """型の違反を件数違反や想定外の属性アクセスへ変換しない。"""
    with pytest.raises(TypeError):
        SqsMessageBatch(messages=messages)


def test_batch_rejects_mutable_message_list():
    """後から要素を変更できるリストは内容が正しくても受け入れない。"""
    with pytest.raises(TypeError):
        SqsMessageBatch(messages=[SqsMessage(UUID(int=1), "{}")])


def test_batch_rejects_duplicate_id_even_with_different_bodies():
    """本文が異なっても結果を一意に対応付けられないIDの重複を拒否する。"""
    event_id = UUID(int=1)
    with pytest.raises(ValueError) as caught:
        SqsMessageBatch(
            messages=(
                SqsMessage(event_id, "PRIVATE_BODY_A"),
                SqsMessage(event_id, "PRIVATE_BODY_B"),
            )
        )
    assert str(event_id) not in str(caught.value)
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("extra", [0, 1])
def test_batch_enforces_combined_body_byte_limit(count, extra):
    """上限ちょうどは許可し、単件でも複数件でも1バイト超過を拒否する。"""
    messages = tuple(
        SqsMessage(
            UUID(int=index + 1),
            "x" * (MAX_MESSAGE_BYTES // count + (extra if index == 0 else 0)),
        )
        for index in range(count)
    )
    if extra:
        with pytest.raises(ValueError):
            SqsMessageBatch(messages=messages)
    else:
        assert SqsMessageBatch(messages=messages).messages == messages


def test_batch_measures_utf8_bytes_instead_of_characters():
    """マルチバイト文字も送信されるUTF-8の実サイズで制限する。"""
    body = "あ" * (MAX_MESSAGE_BYTES // 3 + 1)
    assert len(body) < MAX_MESSAGE_BYTES
    assert len(body.encode("utf-8")) > MAX_MESSAGE_BYTES
    with pytest.raises(ValueError):
        SqsMessageBatch(messages=(SqsMessage(UUID(int=1), body),))


def test_batch_cannot_be_reassigned_appended_or_modified():
    """構築後の置き換え・追加・削除と要素の書き換えを防ぐ。"""
    message = SqsMessage(UUID(int=1), "{}")
    batch = SqsMessageBatch(messages=(message,))
    with pytest.raises(FrozenInstanceError):
        batch.messages = ()
    with pytest.raises(FrozenInstanceError):
        batch.messages += (SqsMessage(UUID(int=2), "{}"),)
    with pytest.raises(FrozenInstanceError):
        del batch.messages
    with pytest.raises(TypeError):
        batch.messages[0] = SqsMessage(UUID(int=2), "{}")
    with pytest.raises(FrozenInstanceError):
        batch.messages[0].body = "changed"
    with pytest.raises(ValueError):
        replace(batch, messages=())
    assert batch.messages == (message,)


def test_batch_display_does_not_expose_body_or_checksum():
    """メッセージを束ねても本文と照合情報を通常表示へ出さない。"""
    message = SqsMessage(UUID(int=1), "PRIVATE_BODY")
    batch = SqsMessageBatch(messages=(message,))
    for display in (str(batch), repr(batch)):
        assert message.body not in display
        assert message.body_md5 not in display
