"""SQSへ送る本文の上限と応答の照合、まとめて送るときのIDの突き合わせを検証する(SDKは呼ばず応答の辞書を直接渡す)。"""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from app.aws.sqs.errors import (
    MessageBodyMismatchError,
    MessageTooLargeError,
    SqsResponseInvalidError,
)
from app.aws.sqs.outgoing_message import (
    OutgoingSqsMessage,
    OutgoingSqsMessageBatch,
    SqsBatchSendResult,
)

# SQSのメッセージ1件と1回のバッチ全体の上限で、どちらもUTF-8のバイト数で数える。
SQS_MAX_BYTES = 1_048_576
EMPTY_JSON_MD5 = "99914b932bd37a50b983c5e7c90ae93b"
OTHER_MD5 = "900150983cd24fb0d6963f7d28e17f72"


def _batch(*ids):
    return OutgoingSqsMessageBatch({id_: OutgoingSqsMessage("{}") for id_ in ids})


def _success(id_="a", checksum=EMPTY_JSON_MD5):
    return {"Id": id_, "MessageId": "message-id", "MD5OfMessageBody": checksum}


def _failure(id_="b", code="AccessDenied"):
    return {"Id": id_, "Code": code, "SenderFault": True}


@pytest.mark.parametrize(
    ("body", "too_large"),
    [
        ("x" * SQS_MAX_BYTES, False),
        ("x" * (SQS_MAX_BYTES - 2) + "あ", True),
    ],
)
def test_message_size_limit_counts_utf8_bytes(body, too_large):
    """本文のUTF-8バイト数が上限を超えるときだけ、作る時点で送れない失敗にする。"""
    if not too_large:
        assert OutgoingSqsMessage(body).body == body
        return
    with pytest.raises(MessageTooLargeError) as caught:
        OutgoingSqsMessage(body)
    assert caught.value.details is None


@pytest.mark.parametrize(
    ("body", "expected_md5"),
    [
        ("{}", "99914b932bd37a50b983c5e7c90ae93b"),
        (' { "text": "日本語\\n\\"引用\\"" }\n', "9d37d07e76129464ee66174bdcaff86a"),
    ],
)
def test_message_derives_md5_from_exact_body(body, expected_md5):
    """本文のUTF-8バイト列からMD5を計算し、照合値を外から受け取らない。"""
    assert OutgoingSqsMessage(body).body_md5 == expected_md5
    with pytest.raises(TypeError):
        OutgoingSqsMessage(body=body, body_md5=expected_md5)  # type: ignore[call-arg]


def test_message_hides_body_and_cannot_be_changed():
    """本文と照合値を表示に出さず、作った後に書き換えられない。"""
    message = OutgoingSqsMessage("PRIVATE_BODY")
    assert "PRIVATE_BODY" not in repr(message)
    assert message.body_md5 not in repr(message)
    with pytest.raises(FrozenInstanceError):
        message.body = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("result", "field", "reason"),
    [
        (None, "response", "invalid_type"),
        ({"MD5OfMessageBody": EMPTY_JSON_MD5}, "message_id", "missing_required_field"),
        (
            {"MessageId": None, "MD5OfMessageBody": EMPTY_JSON_MD5},
            "message_id",
            "invalid_type",
        ),
        (
            {"MessageId": "", "MD5OfMessageBody": EMPTY_JSON_MD5},
            "message_id",
            "empty_required_field",
        ),
        ({"MessageId": "id"}, "body_checksum", "missing_required_field"),
        ({"MessageId": "id", "MD5OfMessageBody": 1}, "body_checksum", "invalid_type"),
        (
            {"MessageId": "id", "MD5OfMessageBody": ""},
            "body_checksum",
            "empty_required_field",
        ),
        *[
            (
                {"MessageId": "id", "MD5OfMessageBody": checksum},
                "body_checksum",
                "invalid_checksum_format",
            )
            for checksum in ["a" * 31, "a" * 33, "g" * 32, "０" * 32, "a" * 32 + "\n"]
        ],
    ],
)
def test_verify_reports_which_field_was_invalid(result, field, reason):
    """応答の形が不正なら、どの項目がどう不正だったかを持たせて応答不正にする。"""
    with pytest.raises(SqsResponseInvalidError) as caught:
        OutgoingSqsMessage("{}").verify(result)
    assert caught.value.details == {"reason": reason, "field": field}


@pytest.mark.parametrize(
    ("checksum", "mismatch"), [(EMPTY_JSON_MD5.upper(), False), (OTHER_MD5, True)]
)
def test_verify_compares_checksum_with_sent_body(checksum, mismatch):
    """大文字小文字を区別せずMD5を比べ、正しい形で値が違うときだけ本文不一致にする。"""
    result = {"MessageId": "id", "MD5OfMessageBody": checksum}
    if not mismatch:
        OutgoingSqsMessage("{}").verify(result)
        return
    with pytest.raises(MessageBodyMismatchError) as caught:
        OutgoingSqsMessage("{}").verify(result)
    assert caught.value.details is None


@pytest.mark.parametrize(
    "bodies",
    [
        [],
        ["{}"] * 11,
        ["x" * (SQS_MAX_BYTES // 2), "x" * (SQS_MAX_BYTES // 2 + 1)],
    ],
)
def test_batch_rejects_count_or_total_size_over_sqs_limits(bodies):
    """1〜10件と本文の合計上限を満たさないまとまりは、送る前に呼び出し違反にする。"""
    with pytest.raises(ValueError):
        OutgoingSqsMessageBatch(
            {str(index): OutgoingSqsMessage(body) for index, body in enumerate(bodies)}
        )


def test_batch_entries_keep_ids_bodies_and_order():
    """SendMessageBatchへ渡す項目は、渡したIDと本文を順序どおりに並べる。"""
    batch = OutgoingSqsMessageBatch(
        {"second": OutgoingSqsMessage("{}"), "first": OutgoingSqsMessage("abc")}
    )
    assert batch.entries() == [
        {"Id": "second", "MessageBody": "{}"},
        {"Id": "first", "MessageBody": "abc"},
    ]


def test_read_matches_entries_by_id_regardless_of_response_order():
    """応答の並びが送信順と違っても、IDで突き合わせて本文の異なるメッセージを取り違えない。"""
    batch = OutgoingSqsMessageBatch(
        {"a": OutgoingSqsMessage("{}"), "b": OutgoingSqsMessage("abc")}
    )
    response = {
        "Successful": [_success("b", OTHER_MD5), _success("a", EMPTY_JSON_MD5)],
        "ResponseMetadata": {"RequestId": "request-id"},
    }
    before = deepcopy(response)
    assert batch.read(response) == SqsBatchSendResult(
        body_mismatched=frozenset(), rejected_codes={}, request_id="request-id"
    )
    assert response == before


def test_read_keeps_mismatch_and_rejection_to_their_own_ids():
    """本文不一致と拒否は、そのIDのメッセージだけの結果にする。"""
    batch = _batch("a", "b", "c")
    response = {
        "Successful": [_success("a"), _success("c", OTHER_MD5)],
        "Failed": [_failure("b", "RequestThrottled")],
    }
    assert batch.read(response) == SqsBatchSendResult(
        body_mismatched=frozenset({"c"}),
        rejected_codes={"b": "RequestThrottled"},
        request_id=None,
    )


@pytest.mark.parametrize("metadata", [None, "private", {}, {"RequestId": 1}])
def test_read_treats_unusable_metadata_as_missing_request_id(metadata):
    """調査用のRequestIdが読めなくても、受付の結果は失わない。"""
    response = {"Successful": [_success()], "ResponseMetadata": metadata}
    assert _batch("a").read(response) == SqsBatchSendResult(
        body_mismatched=frozenset(), rejected_codes={}, request_id=None
    )


@pytest.mark.parametrize(
    ("response", "field", "reason"),
    [
        (None, "response", "invalid_type"),
        ([], "response", "invalid_type"),
        ({"Successful": {}}, "successful_entries", "invalid_type"),
        ({"Failed": None}, "failed_entries", "invalid_type"),
        ({"Successful": [None]}, "successful_entry", "invalid_type"),
        ({"Failed": ["private"]}, "failed_entry", "invalid_type"),
        ({"Successful": [{"MessageId": "id"}]}, "entry_id", "missing_required_field"),
        ({"Successful": [_success(1)]}, "entry_id", "invalid_type"),
        ({"Successful": [_success("")]}, "entry_id", "empty_required_field"),
        ({"Successful": [_success("unknown")]}, "entry_id", "unknown_entry_id"),
        ({"Successful": [_success(), _success()]}, "entry_id", "duplicate_entry_id"),
        (
            {"Successful": [_success()], "Failed": [_failure("a")]},
            "entry_id",
            "duplicate_entry_id",
        ),
        ({"Successful": []}, "entry_id", "missing_entry_id"),
        (
            {"Successful": [{**_success(), "MD5OfMessageBody": "x"}]},
            "body_checksum",
            "invalid_checksum_format",
        ),
        (
            {"Failed": [{"Id": "a", "Code": "AccessDenied"}]},
            "sender_fault",
            "missing_required_field",
        ),
        (
            {"Failed": [{**_failure("a"), "SenderFault": "true"}]},
            "sender_fault",
            "invalid_type",
        ),
        ({"Failed": [{**_failure("a"), "Code": 123}]}, "error_code", "invalid_type"),
        (
            {"Failed": [{**_failure("a"), "Code": ""}]},
            "error_code",
            "empty_required_field",
        ),
    ],
)
def test_read_rejects_malformed_response_as_a_whole(response, field, reason):
    """形やIDの対応が1か所でも不正なら、どこがどう不正かを持たせてまとまり全体を応答不正にする。"""
    with pytest.raises(SqsResponseInvalidError) as caught:
        _batch("a").read(response)
    assert caught.value.details == {"reason": reason, "field": field}


def test_read_does_not_expose_external_strings():
    """拒否コードやRequestIdなど外部由来の文字列を、結果や例外の表示に出さない。"""
    marker = "PRIVATE_PAYLOAD_QUEUE_CREDENTIAL"
    result = _batch("a", "b").read(
        {
            "Successful": [{**_success(), "MessageId": marker, "extra": marker}],
            "Failed": [{**_failure("b", marker), "Message": marker}],
            "ResponseMetadata": {"RequestId": marker},
        }
    )
    assert marker not in repr(result)
    with pytest.raises(SqsResponseInvalidError) as caught:
        _batch("a").read({"Successful": [_success(marker)]})
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value.details)
