"""例外入力の解決、共通探索、上限、変換担当との連携を検証する。"""

import json
import sys
from unittest.mock import Mock

import pytest

from app.log_policy.exceptions import extraction
from app.log_policy.exceptions.types import ConvertedException
from app.shared.errors import ApplicationError

pytestmark = pytest.mark.unit

_SECRET = "synthetic-row-value"


class _UntouchableError(Exception):
    """探索対象から外れた例外を読み取ったらテストを失敗させる。"""

    def __str__(self) -> str:
        pytest.fail("must not stringify the omitted exception")

    def __getattribute__(self, name: str):
        pytest.fail(f"must not inspect the omitted exception: {name}")


def _failure_with_frames(frame_count: int, message: str) -> ValueError:
    """指定した数の関数呼び出しを通り抜けた例外を作る。"""

    def fail(depth: int) -> None:
        if depth:
            fail(depth - 1)
        else:
            raise ValueError(message)

    try:
        fail(frame_count - 2)
    except ValueError as exc:
        return exc
    raise AssertionError("unreachable")


def _failure_with_cause(label: str) -> RuntimeError:
    failure = RuntimeError(f"operation {label} failed")
    failure.__cause__ = ValueError(f"cause {label}")
    return failure


class TestExcInfo:
    """受け取ったexc_infoを解決し、有効な例外だけを抽出する。"""

    def test_exc_info_true_resolves_current_exception(self) -> None:
        """`exc_info=True`を渡すと、exceptで処理中の例外から型とメッセージを取得する。"""
        try:
            raise ValueError("boom")
        except ValueError:
            fields = extraction.extract_exception_fields(True)
        assert fields is not None
        assert fields["error_class"] == "builtins.ValueError"
        assert fields["error_message"] == "boom"

    def test_exc_info_exception_instance_is_described(self) -> None:
        """渡された例外から、型・メッセージ・発生位置を取得する。"""
        try:
            raise RuntimeError("instance form")
        except RuntimeError as exc:
            fields = extraction.extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_class"] == "builtins.RuntimeError"
        assert fields["error_message"] == "instance form"
        assert fields["frames"][-1]["function"].startswith("test_exc_info_exception")

    @pytest.mark.parametrize("value", [None, False], ids=["none", "false"])
    def test_exc_info_absent_yields_nothing(self, value) -> None:
        """exc_info が無い / False のログには例外フィールドを足さない。"""
        assert extraction.extract_exception_fields(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param((str, "raw exception", None), id="type_mismatch"),
            pytest.param(
                (ValueError, ValueError(), "bad traceback"), id="non_traceback"
            ),
        ],
    )
    def test_invalid_exc_info_is_ignored(self, value) -> None:
        """型が合わない tuple は例外フィールドを作らない。"""
        assert extraction.extract_exception_fields(value) is None


class TestSingleException:
    """連鎖のない例外を、外側の例外の項目へ変換する。"""

    def test_exception_without_chain_has_only_top_level_fields(self) -> None:
        """連鎖のない例外は、型・原因文・発生位置だけの形に変換する。"""
        fields = extraction.extract_exception_fields(ValueError("invalid value"))

        assert fields == {
            "error_class": "builtins.ValueError",
            "error_message": "invalid value",
            "frames": [],
        }


class TestRelations:
    """前の例外を、関係の種類つきで外側から順に並べる。"""

    def test_explicit_causes_are_listed_as_cause(self) -> None:
        """明示causeの連鎖を、外側から順にcauseとして並べる。"""
        outer = RuntimeError("fetch failed")
        middle = ValueError("invalid response")
        outer.__cause__ = middle
        middle.__cause__ = OSError("connection refused")

        fields = extraction.extract_exception_fields(outer)

        assert fields == {
            "error_class": "builtins.RuntimeError",
            "error_message": "fetch failed",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "invalid response",
                        "frames": [],
                    },
                },
                {
                    "parent": 0,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.OSError",
                        "error_message": "connection refused",
                        "frames": [],
                    },
                },
            ],
        }

    def test_visible_context_is_listed_as_context(self) -> None:
        """明示causeがなく抑制されていないcontextを、contextとして並べる。"""
        outer = RuntimeError("cleanup failed")
        outer.__context__ = KeyError("missing")

        fields = extraction.extract_exception_fields(outer)

        assert fields == {
            "error_class": "builtins.RuntimeError",
            "error_message": "cleanup failed",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "context",
                    "exception": {
                        "error_class": "builtins.KeyError",
                        "error_message": "'missing'",
                        "frames": [],
                    },
                },
            ],
        }

    def test_explicit_cause_takes_precedence_over_context(self) -> None:
        """causeとcontextの両方があるときは、causeだけを並べる。"""
        outer = RuntimeError("outer")
        outer.__context__ = ValueError("synthetic-private-context")
        outer.__cause__ = ValueError("cause")

        fields = extraction.extract_exception_fields(outer)

        assert fields == {
            "error_class": "builtins.RuntimeError",
            "error_message": "outer",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "cause",
                        "frames": [],
                    },
                },
            ],
        }

    def test_suppressed_context_is_not_listed(self) -> None:
        """from Noneで抑制したcontextは、関連する例外として出力しない。"""
        try:
            try:
                raise ValueError("synthetic-private-context")
            except ValueError:
                raise RuntimeError("outer") from None
        except RuntimeError as exc:
            fields = extraction.extract_exception_fields(exc)

        assert "related_exceptions" not in fields
        assert "synthetic-private-context" not in json.dumps(fields)


class TestGroupMembers:
    """ExceptionGroupのメンバーを、memberとして元の順序で並べる。"""

    def test_member_causes_follow_each_member(self) -> None:
        """メンバーの原因は、そのメンバーの直後に並べる。"""
        member_a = RuntimeError("task A failed")
        member_a.__cause__ = OSError("timeout")
        group = ExceptionGroup("batch failed", [member_a, TypeError("task B failed")])

        fields = extraction.extract_exception_fields(group)

        assert fields == {
            "error_class": "builtins.ExceptionGroup",
            "error_message": "batch failed (2 sub-exceptions)",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "task A failed",
                        "frames": [],
                    },
                },
                {
                    "parent": 0,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.OSError",
                        "error_message": "timeout",
                        "frames": [],
                    },
                },
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.TypeError",
                        "error_message": "task B failed",
                        "frames": [],
                    },
                },
            ],
        }

    def test_group_cause_precedes_members(self) -> None:
        """グループ自体の原因を先に、メンバーを後に並べる。"""
        group = ExceptionGroup(
            "retry failed", [ValueError("attempt 1"), ValueError("attempt 2")]
        )
        group.__cause__ = OSError("connection refused")

        fields = extraction.extract_exception_fields(group)

        assert fields == {
            "error_class": "builtins.ExceptionGroup",
            "error_message": "retry failed (2 sub-exceptions)",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.OSError",
                        "error_message": "connection refused",
                        "frames": [],
                    },
                },
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "attempt 1",
                        "frames": [],
                    },
                },
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "attempt 2",
                        "frames": [],
                    },
                },
            ],
        }


class TestCycles:
    """たどっている経路に戻る参照だけを、循環として止める。"""

    def test_reference_back_to_path_is_marked_as_cycle(self) -> None:
        """経路上の例外に戻る関係は[cycle]として示し、その先へ進まない。"""
        outer = RuntimeError("outer")
        inner = ValueError("inner")
        outer.__cause__ = inner
        inner.__cause__ = outer

        fields = extraction.extract_exception_fields(outer)

        assert fields == {
            "error_class": "builtins.RuntimeError",
            "error_message": "outer",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "inner",
                        "frames": [],
                    },
                },
                {"parent": 0, "relation": "cause", "exception": "[cycle]"},
            ],
        }

    def test_same_cause_held_by_multiple_exceptions_is_not_a_cycle(self) -> None:
        """複数の例外が同じ原因を持っているだけでは循環として扱わず、それぞれに並べる。"""
        shared_cause = ConnectionError("connection failed")
        member_a = RuntimeError("operation A failed")
        member_a.__cause__ = shared_cause
        member_b = RuntimeError("operation B failed")
        member_b.__cause__ = shared_cause
        group = ExceptionGroup("parallel failures", [member_a, member_b])

        fields = extraction.extract_exception_fields(group)

        assert fields == {
            "error_class": "builtins.ExceptionGroup",
            "error_message": "parallel failures (2 sub-exceptions)",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "operation A failed",
                        "frames": [],
                    },
                },
                {
                    "parent": 0,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ConnectionError",
                        "error_message": "connection failed",
                        "frames": [],
                    },
                },
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "operation B failed",
                        "frames": [],
                    },
                },
                {
                    "parent": 2,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ConnectionError",
                        "error_message": "connection failed",
                        "frames": [],
                    },
                },
            ],
        }


class TestTotalLimit:
    """外側・メンバー・原因を合わせた総数を制限する。"""

    def test_exceptions_at_total_limit_are_preserved(self) -> None:
        """同じグループのメンバーは同じ深さとして扱い、総数上限まで残す。"""
        # グループ自身が1件を使う。
        member_count = extraction.EXCEPTION_LIMIT - 1
        members = [ValueError(f"failure {index}") for index in range(member_count)]
        group = ExceptionGroup("parallel failures", members)

        fields = extraction.extract_exception_fields(group)

        assert fields["related_exceptions"] == [
            {
                "parent": None,
                "relation": "member",
                "exception": {
                    "error_class": "builtins.ValueError",
                    "error_message": f"failure {index}",
                    "frames": [],
                },
            }
            for index in range(member_count)
        ]

    def test_exception_beyond_total_limit_is_omitted_without_inspection(self) -> None:
        """上限内のメンバーを残し、1件超えた例外は読まずに省略する。"""
        # グループ自身が1件を使う。
        member_count = extraction.EXCEPTION_LIMIT - 1
        members = [ValueError(f"failure {index}") for index in range(member_count)]
        group = ExceptionGroup("parallel failures", [*members, _UntouchableError()])

        fields = extraction.extract_exception_fields(group)

        assert fields["related_exceptions"] == [
            *[
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": f"failure {index}",
                        "frames": [],
                    },
                }
                for index in range(member_count)
            ],
            {"parent": None, "relation": "member", "exception": "[limit]"},
        ]

    def test_total_limit_inside_group_omits_causes_and_remaining_members(self) -> None:
        """総数上限に達したら、さらにネストした例外と残りのグループメンバーを読まず省略する。"""
        # 外側と内側のグループ自身も、それぞれ1件として数える。
        group_count = 2
        member_count = extraction.EXCEPTION_LIMIT - group_count
        members = [ValueError(f"failure {index}") for index in range(member_count)]
        members[-1].__cause__ = _UntouchableError()
        inner = ExceptionGroup("inner failures", [*members, _UntouchableError()])
        outer = ExceptionGroup("outer failures", [inner, _UntouchableError()])

        fields = extraction.extract_exception_fields(outer)

        assert fields["related_exceptions"] == [
            {
                "parent": None,
                "relation": "member",
                "exception": {
                    "error_class": "builtins.ExceptionGroup",
                    "error_message": (
                        f"inner failures ({member_count + 1} sub-exceptions)"
                    ),
                    "frames": [],
                },
            },
            *[
                {
                    "parent": 0,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": f"failure {index}",
                        "frames": [],
                    },
                }
                for index in range(member_count)
            ],
            {"parent": member_count, "relation": "cause", "exception": "[limit]"},
            {"parent": 0, "relation": "member", "exception": "[limit]"},
            {"parent": None, "relation": "member", "exception": "[limit]"},
        ]

    def test_member_beyond_total_limit_is_not_converted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """総数上限に達したら、残りのメンバーには変換処理を呼ばない。"""
        # グループ自身と最初のメンバーで上限2件に達する。
        monkeypatch.setattr(extraction, "EXCEPTION_LIMIT", 2)
        included_member = RuntimeError("included failure")
        omitted_member = RuntimeError("omitted failure")
        group = ExceptionGroup(
            "parallel failures",
            [included_member, omitted_member],
        )
        converter = Mock(wraps=extraction.convert_exception)
        extraction.extract_exception_fields(group, exception_converter=converter)

        converted_exceptions = [call.args[0] for call in converter.call_args_list]
        assert included_member in converted_exceptions
        assert omitted_member not in converted_exceptions

    def test_members_and_causes_at_total_limit_are_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """グループ・メンバー・原因の合計が総数上限ちょうどなら、すべて出力する。"""
        # グループ自身1件、メンバー2件、それぞれの原因2件がちょうど収まる上限にする。
        monkeypatch.setattr(extraction, "EXCEPTION_LIMIT", 5)
        group = ExceptionGroup(
            "parallel failures",
            [_failure_with_cause("A"), _failure_with_cause("B")],
        )

        fields = extraction.extract_exception_fields(group)

        assert fields == {
            "error_class": "builtins.ExceptionGroup",
            "error_message": "parallel failures (2 sub-exceptions)",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "operation A failed",
                        "frames": [],
                    },
                },
                {
                    "parent": 0,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "cause A",
                        "frames": [],
                    },
                },
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "operation B failed",
                        "frames": [],
                    },
                },
                {
                    "parent": 2,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "cause B",
                        "frames": [],
                    },
                },
            ],
        }

    def test_member_beyond_shared_total_limit_is_marked_without_inspection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """上限に達したら、後続メンバーを読まず取得済み情報と省略を残す。"""
        # グループ1 + (メンバー + 原因) × 2 = 5で、後続メンバーは上限外。
        monkeypatch.setattr(extraction, "EXCEPTION_LIMIT", 5)
        group = ExceptionGroup(
            "parallel failures",
            [_failure_with_cause("A"), _failure_with_cause("B"), _UntouchableError()],
        )

        fields = extraction.extract_exception_fields(group)

        assert fields == {
            "error_class": "builtins.ExceptionGroup",
            "error_message": "parallel failures (3 sub-exceptions)",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "operation A failed",
                        "frames": [],
                    },
                },
                {
                    "parent": 0,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "cause A",
                        "frames": [],
                    },
                },
                {
                    "parent": None,
                    "relation": "member",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "operation B failed",
                        "frames": [],
                    },
                },
                {
                    "parent": 2,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.ValueError",
                        "error_message": "cause B",
                        "frames": [],
                    },
                },
                {"parent": None, "relation": "member", "exception": "[limit]"},
            ],
        }


class TestConversionBoundary:
    """変換担当の結果を組み立て、原因の集約と失敗を扱う。"""

    def test_exception_extraction_leaves_leak_prevention_to_common_preparation(
        self,
    ) -> None:
        """例外抽出だけの入口は情報漏洩防止を行わず、原文の原因文を返す。"""
        # 合成値を分割し、秘密検出ツールの規則に一致させない。
        gemini_key = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
        message = f"request with {gemini_key} failed"
        fields = extraction.extract_exception_fields(ValueError(message))
        assert fields == {
            "error_class": "builtins.ValueError",
            "error_message": message,
            "frames": [],
        }

    def test_broken_exception_string_keeps_type_and_frame(self) -> None:
        """`__str__` が失敗しても型と発生箇所を残し、message は固定文にする。"""

        class BrokenError(Exception):
            def __str__(self):
                raise RuntimeError(_SECRET)

        try:
            raise BrokenError()
        except BrokenError as exc:
            fields = extraction.extract_exception_fields(exc)
        assert fields is not None
        assert fields["error_class"].endswith("BrokenError")
        assert fields["error_message"] == "[exception message unavailable]"
        assert (
            fields["frames"][-1]["function"]
            == "test_broken_exception_string_keeps_type_and_frame"
        )
        assert _SECRET not in json.dumps(fields)

    def test_broken_message_preserves_inner_cause(self) -> None:
        """外側の文字列化が壊れていても内側の例外を展開する。"""

        class BrokenError(Exception):
            def __str__(self):
                raise RuntimeError("synthetic-private-value")

        outer = BrokenError()
        outer.__cause__ = ValueError("inner")
        fields = extraction.extract_exception_fields(outer)
        assert fields["error_message"] == "[exception message unavailable]"
        assert fields["related_exceptions"][0]["exception"]["error_message"] == "inner"

    def test_aggregated_cause_is_not_followed(self) -> None:
        """変換担当が内部原因を集約済みとした場合、その原因へ進まない。"""
        outer = RuntimeError("original message")
        outer.__cause__ = _UntouchableError()
        converter = Mock(
            return_value=ConvertedException(
                message="converted message",
                cause_is_aggregated=True,
            )
        )
        fields = extraction.extract_exception_fields(
            outer, exception_converter=converter
        )

        assert fields == {
            "error_class": "builtins.RuntimeError",
            "error_message": "converted message",
            "frames": [],
        }
        converter.assert_called_once_with(outer)

    def test_details_of_cause_stay_in_its_exception(self) -> None:
        """原因の診断情報は、その原因のexceptionの中に残し、外側へ引き上げない。"""
        outer = RuntimeError("operation failed")
        outer.__cause__ = ApplicationError(
            "fetch failed", details={"reason": "network"}
        )

        fields = extraction.extract_exception_fields(outer)

        assert fields == {
            "error_class": "builtins.RuntimeError",
            "error_message": "operation failed",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "app.shared.errors.ApplicationError",
                        "error_message": "fetch failed",
                        "frames": [],
                        "error_details": {"reason": "network"},
                    },
                },
            ],
        }

    def test_aggregated_related_exception_is_the_last_one_listed(self) -> None:
        """関連する例外が内部原因を集約済みとした場合、その例外までを並べ、内部原因は読み取らない。"""
        outer = RuntimeError("operation failed")
        database_error = RuntimeError("database failed")
        outer.__cause__ = database_error
        database_error.__cause__ = _UntouchableError()

        def convert(exc: BaseException) -> ConvertedException:
            if exc is database_error:
                return ConvertedException(
                    message="database failed", cause_is_aggregated=True
                )
            return ConvertedException(message=str(exc))

        fields = extraction.extract_exception_fields(outer, exception_converter=convert)

        assert fields == {
            "error_class": "builtins.RuntimeError",
            "error_message": "operation failed",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.RuntimeError",
                        "error_message": "database failed",
                        "frames": [],
                    },
                },
            ],
        }


class TestConfiguredConverter:
    """指定した変換担当を、探索可能な例外だけに適用する。"""

    def test_converter_is_shared_by_root_causes_context_and_members(self) -> None:
        """外側・原因・context・グループの各例外へ同じ変換担当を引き継ぐ。"""
        inner = ValueError("inner")
        member = RuntimeError("member")
        member.__context__ = inner
        sibling = TypeError("sibling")
        group = ExceptionGroup("failures", [member, sibling])
        outer = RuntimeError("outer")
        outer.__cause__ = group
        converter = Mock(return_value=ConvertedException(message="converted"))

        fields = extraction.extract_exception_fields(
            outer, exception_converter=converter
        )

        assert [call.args[0] for call in converter.call_args_list] == [
            outer,
            group,
            member,
            inner,
            sibling,
        ]
        assert fields["error_message"] == "converted"
        assert [
            element["exception"]["error_message"]
            for element in fields["related_exceptions"]
        ] == ["converted", "converted", "converted", "converted"]

    def test_cycle_does_not_convert_same_path_again(self) -> None:
        """現在の経路に戻った例外は循環として示し、再変換しない。"""
        outer = RuntimeError("outer")
        inner = ValueError("inner")
        outer.__cause__ = inner
        inner.__cause__ = outer
        converter = Mock(return_value=ConvertedException(message="converted"))

        fields = extraction.extract_exception_fields(
            outer, exception_converter=converter
        )

        assert [call.args[0] for call in converter.call_args_list] == [outer, inner]
        assert fields["related_exceptions"][1] == {
            "parent": 0,
            "relation": "cause",
            "exception": "[cycle]",
        }


class TestFrames:
    """frameから取得する情報と件数上限を守る。"""

    def test_frames_carry_only_file_function_line(self) -> None:
        """frame は file / function / line のみで、locals やソース行は含めない。"""

        def _raise() -> None:
            raise ValueError(_SECRET)

        try:
            _raise()
        except ValueError:
            fields = extraction.extract_exception_fields(sys.exc_info())
        assert fields is not None
        innermost = fields["frames"][-1]
        assert set(innermost) == {"file", "function", "line"}
        assert innermost["function"] == "_raise"
        assert _SECRET not in repr(fields["frames"])

    def test_frames_up_to_total_limit_are_kept_for_single_exception(self) -> None:
        """1件の例外でも、frame数の合計の上限までは全件を残す。"""
        exc = _failure_with_frames(extraction.FRAME_TOTAL_LIMIT, "failed")

        fields = extraction.extract_exception_fields(exc)

        assert len(fields["frames"]) == extraction.FRAME_TOTAL_LIMIT

    def test_frames_beyond_total_are_replaced_only_for_that_exception(self) -> None:
        """合計に収まらない例外のframesだけを[limit]にし、先に並べた例外のframesは残す。"""
        # 外側が合計の半分を使い、原因は残りより1件多い。
        outer_frames = extraction.FRAME_TOTAL_LIMIT // 2
        cause_frames = extraction.FRAME_TOTAL_LIMIT - outer_frames + 1
        outer = _failure_with_frames(outer_frames, "outer")
        outer.__cause__ = _failure_with_frames(cause_frames, "cause")

        fields = extraction.extract_exception_fields(outer)

        assert len(fields["frames"]) == outer_frames
        assert fields["related_exceptions"][0]["exception"]["frames"] == "[limit]"

    def test_later_frames_that_fit_remaining_total_are_kept(self) -> None:
        """収まらなかった例外の後でも、残りの合計に収まるframesは残す。"""
        # 外側が合計の半分を使い、中間は残りより1件多く、内側は残りちょうど。
        outer_frames = extraction.FRAME_TOTAL_LIMIT // 2
        remaining_frames = extraction.FRAME_TOTAL_LIMIT - outer_frames
        outer = _failure_with_frames(outer_frames, "outer")
        middle = _failure_with_frames(remaining_frames + 1, "middle")
        outer.__cause__ = middle
        middle.__cause__ = _failure_with_frames(remaining_frames, "inner")

        related = extraction.extract_exception_fields(outer)["related_exceptions"]

        assert related[0]["exception"]["frames"] == "[limit]"
        assert len(related[1]["exception"]["frames"]) == remaining_frames


class TestTextLimits:
    """例外側が出力する文字列の長さと、文字数の合計を制限する。"""

    def test_message_at_text_length_limit_is_kept(self) -> None:
        """単一文字列の上限ちょうどの原因文は、そのまま残す。"""
        message = "m" * extraction.TEXT_LENGTH_LIMIT

        fields = extraction.extract_exception_fields(ValueError(message))

        assert fields["error_message"] == message

    def test_message_over_text_length_limit_is_replaced(self) -> None:
        """単一文字列の上限を超える原因文は、切り詰めずに全体を[limit]にする。"""
        message = "m" * (extraction.TEXT_LENGTH_LIMIT + 1)

        fields = extraction.extract_exception_fields(ValueError(message))

        assert fields["error_message"] == "[limit]"

    def test_class_name_over_text_length_limit_is_replaced(self) -> None:
        """単一文字列の上限を超える型名は[limit]にする。"""
        long_named_error = type(
            "E" * (extraction.TEXT_LENGTH_LIMIT + 1), (Exception,), {}
        )

        fields = extraction.extract_exception_fields(long_named_error("failed"))

        assert fields["error_class"] == "[limit]"

    def test_details_value_over_text_length_limit_is_replaced(self) -> None:
        """単一文字列の上限を超える診断情報の値だけを[limit]にし、他の値は残す。"""
        long_value = "r" * (extraction.TEXT_LENGTH_LIMIT + 1)
        exc = ApplicationError("failed", details={"reason": long_value, "code": "c"})

        fields = extraction.extract_exception_fields(exc)

        assert fields["error_details"] == {"reason": "[limit]", "code": "c"}

    def test_details_with_key_over_text_length_limit_are_replaced(self) -> None:
        """単一文字列の上限を超えるキーを含む診断情報は、キーを置き換えられないため全体を[limit]にする。"""
        long_key = "k" * (extraction.TEXT_LENGTH_LIMIT + 1)
        exc = ApplicationError("failed", details={long_key: "value"})

        fields = extraction.extract_exception_fields(exc)

        assert fields["error_details"] == "[limit]"

    def test_text_at_total_limit_is_kept(self) -> None:
        """文字数の合計が上限ちょうどなら、すべての文字列を残す。"""
        # 型名と原因文で1件あたり単一文字列の上限ちょうどにし、合計の上限まで並べる。
        # 前提: 合計の上限は単一文字列の上限の整数倍で、その件数は件数の上限に収まる。
        message = "m" * (extraction.TEXT_LENGTH_LIMIT - len("builtins.ValueError"))
        count = extraction.TEXT_TOTAL_LIMIT // extraction.TEXT_LENGTH_LIMIT
        chain = [ValueError(message) for _ in range(count)]
        for parent, cause in zip(chain, chain[1:]):
            parent.__cause__ = cause

        fields = extraction.extract_exception_fields(chain[0])

        assert fields["error_message"] == message
        assert [
            element["exception"]["error_message"]
            for element in fields["related_exceptions"]
        ] == [message] * (count - 1)

    def test_text_beyond_total_is_replaced_and_later_text_that_fits_is_kept(
        self,
    ) -> None:
        """合計に収まらない文字列だけを[limit]にし、その後に収まる文字列は残す。"""
        # 型名と原因文で1件あたり単一文字列の上限ちょうどにし、
        # 最後の1件だけ1文字多くする。
        # 前提: 合計の上限は単一文字列の上限の整数倍で、その件数は件数の上限に収まる。
        message = "m" * (extraction.TEXT_LENGTH_LIMIT - len("builtins.ValueError"))
        count = extraction.TEXT_TOTAL_LIMIT // extraction.TEXT_LENGTH_LIMIT
        chain = [ValueError(message) for _ in range(count - 1)]
        chain.append(ValueError(message + "m"))
        chain.append(ValueError("x"))
        for parent, cause in zip(chain, chain[1:]):
            parent.__cause__ = cause

        related = extraction.extract_exception_fields(chain[0])["related_exceptions"]

        assert related[count - 2]["exception"]["error_message"] == "[limit]"
        assert related[count - 1]["exception"] == {
            "error_class": "builtins.ValueError",
            "error_message": "x",
            "frames": [],
        }


class TestDetailsLimit:
    """error_detailsの項目数を、例外全体の合計で制限する。"""

    def test_details_at_total_item_limit_are_kept(self) -> None:
        """診断情報の項目数の合計が上限ちょうどなら、すべて残す。"""
        # 外側と原因で、項目数の上限を分け合う。
        outer_items = extraction.DETAILS_ITEM_LIMIT // 2
        cause_items = extraction.DETAILS_ITEM_LIMIT - outer_items
        outer_details = {f"outer_{index}": index for index in range(outer_items)}
        cause_details = {f"cause_{index}": index for index in range(cause_items)}
        outer = ApplicationError("outer", details=outer_details)
        outer.__cause__ = ApplicationError("cause", details=cause_details)

        fields = extraction.extract_exception_fields(outer)

        assert fields["error_details"] == outer_details
        assert fields["related_exceptions"][0]["exception"]["error_details"] == (
            cause_details
        )

    def test_details_beyond_total_are_replaced_and_later_details_that_fit_are_kept(
        self,
    ) -> None:
        """合計に収まらない診断情報だけを全体[limit]にし、その後に収まる診断情報は残す。"""
        # 外側が上限の半分を使い、中間は残りより1項目多く、内側は残りちょうど。
        outer_items = extraction.DETAILS_ITEM_LIMIT // 2
        remaining_items = extraction.DETAILS_ITEM_LIMIT - outer_items
        inner_details = {f"inner_{index}": index for index in range(remaining_items)}
        outer = ApplicationError(
            "outer", details={f"outer_{index}": index for index in range(outer_items)}
        )
        middle = ApplicationError(
            "middle",
            details={f"middle_{index}": index for index in range(remaining_items + 1)},
        )
        outer.__cause__ = middle
        middle.__cause__ = ApplicationError("inner", details=inner_details)

        related = extraction.extract_exception_fields(outer)["related_exceptions"]

        assert related[0]["exception"]["error_details"] == "[limit]"
        assert related[1]["exception"]["error_details"] == inner_details

    def test_list_elements_in_details_are_counted(self) -> None:
        """配列の要素と、その中の辞書のキーも項目として数える。"""
        # kind・reason・issuesの3項目に、
        # issuesの要素1件ごとに要素とキー2件の3項目が加わる。
        # 合計が上限を1つ以上超える最小の件数にする。
        issue_count = (extraction.DETAILS_ITEM_LIMIT - 3) // 3 + 1
        details = {
            "kind": "application_validation",
            "reason": "invalid_payload",
            "issues": [
                {"field": f"payload.field_{index}", "code": "invalid_type"}
                for index in range(issue_count)
            ],
        }
        converter = Mock(
            return_value=ConvertedException(message="failed", error_details=details)
        )

        fields = extraction.extract_exception_fields(
            ValueError("failed"), exception_converter=converter
        )

        assert fields["error_details"] == "[limit]"
