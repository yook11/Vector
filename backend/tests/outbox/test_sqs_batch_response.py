"""SQS応答の形式・ID対応・本文照合を独立した契約として検証する。"""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.outbox.publish_errors import (
    PublishIntegrityError,
    PublishIntegrityReason,
    PublishResponseInvalidReason,
)
from app.outbox.publisher import PublishFailed, PublishSucceeded
from app.outbox.sqs_batch_response import (
    SqsBatchResponse,
    SqsFailedEntry,
    SqsSuccessfulEntry,
    decode_sqs_batch_response,
    results_from_sqs_batch_response,
    validate_sqs_batch_event_ids,
)
from app.outbox.sqs_message import SqsMessage
from app.outbox.sqs_message_batch import SqsMessageBatch
from app.outbox.sqs_response_errors import InvalidSqsBatchResponse, SqsResponseField

EVENT_ID = str(UUID(int=1))
SECOND_ID = str(UUID(int=2))
EMPTY_JSON_MD5 = "99914b932bd37a50b983c5e7c90ae93b"
OTHER_MD5 = "900150983cd24fb0d6963f7d28e17f72"


def successful(id_=EVENT_ID, checksum=EMPTY_JSON_MD5):
    return {"Id": id_, "MessageId": "message-id", "MD5OfMessageBody": checksum}


def failed(id_=SECOND_ID):
    return {"Id": id_, "Code": "AccessDenied", "SenderFault": True}


def test_decoding_returns_immutable_values_without_retaining_raw_response():
    """入力の辞書を後から変更しても検証済み応答の値は変わらない。"""
    raw = {
        "Successful": [successful(checksum=EMPTY_JSON_MD5.upper())],
        "Failed": [failed()],
        "ResponseMetadata": {"RequestId": "request-id"},
    }
    before = deepcopy(raw)
    response = decode_sqs_batch_response(raw)
    assert raw == before
    assert response == SqsBatchResponse(
        successful=(SqsSuccessfulEntry(EVENT_ID, "message-id", EMPTY_JSON_MD5),),
        failed=(SqsFailedEntry(SECOND_ID, "AccessDenied", True),),
        request_id="request-id",
    )
    raw["Successful"][0]["Id"] = "changed"
    raw["Failed"].clear()
    assert response.successful[0].id == EVENT_ID
    assert len(response.failed) == 1
    for obj, field, value in (
        (response, "successful", ()),
        (response.successful[0], "id", "changed"),
        (response.failed[0], "sender_fault", False),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)


@pytest.mark.parametrize("metadata", [None, "private", {}, {"RequestId": 1}])
def test_optional_fields_do_not_invalidate_required_response(metadata):
    """省略した一覧は空、調査情報の欠損はNoneとして独立して扱う。"""
    decoded = decode_sqs_batch_response({"ResponseMetadata": metadata})
    assert decoded == SqsBatchResponse((), (), None)


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        (None, "response"),
        ([], "response"),
        ("private", "response"),
        ({"Successful": {}}, "successful_entries"),
        ({"Failed": None}, "failed_entries"),
        ({"Successful": [None]}, "successful_entry"),
        ({"Failed": ["private"]}, "failed_entry"),
    ],
)
def test_invalid_containers_raise_fixed_exception(raw, field):
    """構造違反の例外に応答の自由文を取り込まない。"""
    with pytest.raises(InvalidSqsBatchResponse) as caught:
        decode_sqs_batch_response(raw)
    assert caught.value.args == ()
    assert caught.value.reason is PublishResponseInvalidReason.INVALID_TYPE
    assert caught.value.field.value == field
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "group,key",
    [
        ("Successful", "Id"),
        ("Successful", "MessageId"),
        ("Successful", "MD5OfMessageBody"),
        ("Failed", "Id"),
        ("Failed", "Code"),
    ],
)
@pytest.mark.parametrize("value", [None, "", 1, True, [], {}])
def test_required_strings_reject_missing_or_invalid_values(group, key, value):
    """必須文字列の型と空文字を境界で検証する。"""
    entry = successful() if group == "Successful" else failed()
    entry[key] = value
    expected_field = {
        "Id": "entry_id",
        "MessageId": "message_id",
        "MD5OfMessageBody": "body_checksum",
        "Code": "error_code",
    }[key]
    with pytest.raises(InvalidSqsBatchResponse) as caught:
        decode_sqs_batch_response({group: [entry]})
    assert caught.value.reason.value == (
        "empty_required_field" if value == "" else "invalid_type"
    )
    assert caught.value.field.value == expected_field
    entry.pop(key)
    with pytest.raises(InvalidSqsBatchResponse) as caught:
        decode_sqs_batch_response({group: [entry]})
    assert caught.value.reason is PublishResponseInvalidReason.MISSING_REQUIRED_FIELD
    assert caught.value.field.value == expected_field


@pytest.mark.parametrize(
    "value", ["a" * 31, "a" * 33, "g" * 32, "０" * 32, "a" * 32 + "\n"]
)
def test_invalid_md5_format_is_not_an_integrity_mismatch(value):
    """チェックサムの形式不正を、照合した結果の不一致と混同しない。"""
    with pytest.raises(InvalidSqsBatchResponse) as caught:
        results_from_sqs_batch_response(
            {"Successful": [successful(checksum=value)]},
            batch=SqsMessageBatch(messages=(SqsMessage(UUID(EVENT_ID), "{}"),)),
        )
    assert caught.value.reason is PublishResponseInvalidReason.INVALID_CHECKSUM_FORMAT
    assert caught.value.field is SqsResponseField.BODY_CHECKSUM


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_sender_fault_requires_actual_boolean(value):
    """真偽値へ暗黙変換せずAWSの応答型を確認する。"""
    entry = failed()
    entry["SenderFault"] = value
    with pytest.raises(InvalidSqsBatchResponse) as caught:
        decode_sqs_batch_response({"Failed": [entry]})
    assert caught.value.reason is PublishResponseInvalidReason.INVALID_TYPE
    assert caught.value.field is SqsResponseField.SENDER_FAULT
    entry.pop("SenderFault")
    with pytest.raises(InvalidSqsBatchResponse) as caught:
        decode_sqs_batch_response({"Failed": [entry]})
    assert caught.value.reason is PublishResponseInvalidReason.MISSING_REQUIRED_FIELD
    assert caught.value.field is SqsResponseField.SENDER_FAULT


def test_external_strings_are_not_exposed_or_unnecessarily_retained():
    """応答型のreprへ自由文を出さず、Messageや未使用項目を保持しない。"""
    marker = "PRIVATE_PAYLOAD_QUEUE_CREDENTIAL"
    response = decode_sqs_batch_response(
        {
            "Successful": [
                {**successful(marker), "MessageId": marker, "extra": marker}
            ],
            "Failed": [{**failed(marker), "Code": marker, "Message": marker}],
            "ResponseMetadata": {"RequestId": marker},
        }
    )
    assert marker not in repr(response)
    assert marker not in repr(response.successful[0])
    assert marker not in repr(response.failed[0])
    assert EMPTY_JSON_MD5 not in repr(response)
    assert not hasattr(response.successful[0], "extra")
    assert not hasattr(response.failed[0], "message")


@pytest.mark.parametrize("case", ["missing", "unknown", "duplicate", "overlap"])
def test_id_matching_is_separate_from_valid_response_shape(case):
    """項目の形式が正しくても送信IDとの対応が不正なら拒否する。"""
    raw = {"Successful": [successful()], "Failed": [failed()]}
    if case == "missing":
        raw["Failed"] = []
    elif case == "unknown":
        raw["Failed"][0]["Id"] = str(UUID(int=3))
    elif case == "duplicate":
        raw["Failed"] *= 2
    else:
        raw["Failed"].append(failed(EVENT_ID))
    decoded = decode_sqs_batch_response(raw)
    with pytest.raises(InvalidSqsBatchResponse) as caught:
        validate_sqs_batch_event_ids(decoded, event_ids={EVENT_ID, SECOND_ID})
    assert (
        caught.value.reason.value
        == {
            "missing": "missing_entry_id",
            "unknown": "unknown_entry_id",
            "duplicate": "duplicate_entry_id",
            "overlap": "duplicate_entry_id",
        }[case]
    )
    assert caught.value.field is SqsResponseField.ENTRY_ID


def test_successes_are_matched_by_id_before_comparing_checksums():
    """応答順が逆でも、本文の異なるイベント同士を取り違えない。"""
    response = {
        "Successful": [
            successful(SECOND_ID, OTHER_MD5),
            successful(EVENT_ID, EMPTY_JSON_MD5.upper()),
        ]
    }
    messages = (
        SqsMessage(UUID(EVENT_ID), "{}"),
        SqsMessage(UUID(SECOND_ID), "abc"),
    )
    before = deepcopy(response)
    result = results_from_sqs_batch_response(
        response, batch=SqsMessageBatch(messages=messages)
    )
    assert result == {
        message.event_id: PublishSucceeded(message.event_id) for message in messages
    }
    assert response == before


@pytest.mark.parametrize("request_id", [None, "request-id"])
def test_checksum_mismatch_only_fails_the_corresponding_event(request_id):
    """正しい形式のMD5不一致だけを専用の失敗にする。"""
    response = {
        "Successful": [successful(), successful(SECOND_ID, OTHER_MD5)],
    }
    if request_id is not None:
        response["ResponseMetadata"] = {"RequestId": request_id}
    result = results_from_sqs_batch_response(
        response,
        batch=SqsMessageBatch(
            messages=(
                SqsMessage(UUID(EVENT_ID), "{}"),
                SqsMessage(UUID(SECOND_ID), "{}"),
            ),
        ),
    )
    assert result[UUID(EVENT_ID)] == PublishSucceeded(UUID(EVENT_ID))
    failure = result[UUID(SECOND_ID)]
    assert isinstance(failure, PublishFailed)
    assert isinstance(failure.error, PublishIntegrityError)
    assert failure.error.reason is PublishIntegrityReason.BODY_CHECKSUM_MISMATCH
    assert failure.error.request_id == request_id
    assert vars(failure.error) == {
        "reason": PublishIntegrityReason.BODY_CHECKSUM_MISMATCH,
        "request_id": request_id,
    }
