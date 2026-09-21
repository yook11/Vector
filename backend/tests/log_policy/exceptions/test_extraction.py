"""例外入力の解決、共通探索、上限、変換担当との連携を検証する。"""

import json
import sys
from unittest.mock import Mock

import pytest

from app.log_policy.budget import TEXT_LIMIT
from app.log_policy.exceptions import extraction
from app.log_policy.exceptions.conversion import ConvertedException

pytestmark = pytest.mark.unit

_SECRET = "synthetic-row-value"


class _UntouchableError(Exception):
    """探索対象から外れた例外を読み取ったらテストを失敗させる。"""

    def __str__(self) -> str:
        pytest.fail("must not stringify the omitted exception")

    def __getattribute__(self, name: str):
        pytest.fail(f"must not inspect the omitted exception: {name}")


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


class TestCauseRelationships:
    """Pythonの例外連鎖に従って、記録する原因を選ぶ。"""

    def test_visible_context_is_followed(self) -> None:
        """明示causeがなければ抑制されていないcontextを記録する。"""
        outer = RuntimeError("outer")
        outer.__context__ = ValueError("inner")
        assert extraction.extract_exception_fields(outer)["causes"] == [
            {
                "error_class": "builtins.ValueError",
                "error_message": "inner",
                "frames": [],
            }
        ]

    def test_explicit_cause_takes_precedence_over_context(self) -> None:
        """明示causeがあるときは別のcontextを展開しない。"""
        outer = RuntimeError("outer")
        outer.__cause__ = ValueError("cause")
        outer.__context__ = ValueError("synthetic-private-context")
        fields = extraction.extract_exception_fields(outer)
        assert fields["causes"][0]["error_message"] == "cause"
        assert "synthetic-private-context" not in json.dumps(fields)

    def test_raise_from_none_hides_context(self) -> None:
        """from Noneで表示を抑制した元の例外は、原因として辿らず出力しない。"""
        try:
            try:
                raise ValueError("synthetic-private-context")
            except ValueError:
                raise RuntimeError("outer") from None
        except RuntimeError as exc:
            fields = extraction.extract_exception_fields(exc)
        assert "causes" not in fields
        assert "synthetic-private-context" not in json.dumps(fields)

    def test_cyclic_cause_uses_marker(self) -> None:
        """原因の循環参照を検出したら、[cycle]を出力して探索を止める。"""
        outer = RuntimeError("outer")
        inner = ValueError("inner")
        outer.__cause__ = inner
        inner.__cause__ = outer
        assert (
            extraction.extract_exception_fields(outer)["causes"][0]["causes"]
            == "[cycle]"
        )


class TestGroupExpansion:
    """グループのメンバーと、それぞれの原因を取り出す。"""

    def test_exception_group_cause_preserves_its_members(self) -> None:
        """例外の原因がExceptionGroupの場合も、その各メンバーを記録する。"""
        group = ExceptionGroup(
            "parallel failures",
            [ValueError("failure A"), TypeError("failure B")],
        )
        outer = RuntimeError("operation failed")
        outer.__cause__ = group

        fields = extraction.extract_exception_fields(outer)

        assert fields["causes"][0]["exceptions"] == [
            {
                "error_class": "builtins.ValueError",
                "error_message": "failure A",
                "frames": [],
            },
            {
                "error_class": "builtins.TypeError",
                "error_message": "failure B",
                "frames": [],
            },
        ]

    def test_shared_cause_is_preserved_for_each_group_member(self) -> None:
        """複数の例外が同じ原因インスタンスを共有しても、循環扱いせずそれぞれに記録する。"""
        shared_cause = ConnectionError("connection failed")

        member_a = RuntimeError("operation A failed")
        member_a.__cause__ = shared_cause

        member_b = RuntimeError("operation B failed")
        member_b.__cause__ = shared_cause

        group = ExceptionGroup("parallel failures", [member_a, member_b])

        fields = extraction.extract_exception_fields(group)

        expected_cause = [
            {
                "error_class": "builtins.ConnectionError",
                "error_message": "connection failed",
                "frames": [],
            }
        ]
        assert fields["exceptions"][0]["causes"] == expected_cause
        assert fields["exceptions"][1]["causes"] == expected_cause


class TestDepthLimit:
    """原因やグループの子を深さ上限まで抽出し、その先は読み取らない。"""

    def test_chain_at_depth_limit_retains_last_exception(self) -> None:
        """深さ上限ちょうどの例外まで、原因の情報をすべて残す。"""
        outer = RuntimeError("last")
        for _ in range(extraction.CAUSE_DEPTH_LIMIT):
            parent = RuntimeError("parent")
            parent.__cause__ = outer
            outer = parent
        fields = extraction.extract_exception_fields(outer)
        for _ in range(extraction.CAUSE_DEPTH_LIMIT):
            fields = fields["causes"][0]
        assert fields["error_message"] == "last"
        assert "causes" not in fields

    def test_chain_over_depth_limit_uses_marker(self) -> None:
        """深さ上限を超えた例外は読み取らず、その先を[limit]で示す。"""

        outer = _UntouchableError()
        for _ in range(extraction.CAUSE_DEPTH_LIMIT + 1):
            parent = RuntimeError("parent")
            parent.__cause__ = outer
            outer = parent
        fields = extraction.extract_exception_fields(outer)
        for _ in range(extraction.CAUSE_DEPTH_LIMIT):
            fields = fields["causes"][0]
        assert fields["causes"] == "[limit]"

    def test_nested_group_members_beyond_depth_limit_are_not_inspected(self) -> None:
        """グループの中でさらにネストして深さ上限を超えたら、子を読まず省略する。"""
        group = ExceptionGroup("deepest failures", [_UntouchableError()])
        for _ in range(extraction.CAUSE_DEPTH_LIMIT):
            group = ExceptionGroup("nested failures", [group])

        fields = extraction.extract_exception_fields(group)

        for _ in range(extraction.CAUSE_DEPTH_LIMIT):
            fields = fields["exceptions"][0]
        assert fields["error_message"] == "deepest failures (1 sub-exception)"
        assert fields["exceptions"] == ["[limit]"]


class TestTotalLimit:
    """外側・メンバー・原因を合わせた総数を制限する。"""

    def test_exceptions_at_total_limit_are_preserved(self) -> None:
        """同じグループのメンバーは同じ深さとして扱い、総数上限まで残す。"""
        # グループ自身が1件を使う。
        member_count = extraction.EXCEPTION_LIMIT - 1
        members = [ValueError(f"failure {index}") for index in range(member_count)]
        group = ExceptionGroup("parallel failures", members)

        fields = extraction.extract_exception_fields(group)

        assert fields["exceptions"] == [
            {
                "error_class": "builtins.ValueError",
                "error_message": f"failure {index}",
                "frames": [],
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

        assert fields["exceptions"] == [
            *[
                {
                    "error_class": "builtins.ValueError",
                    "error_message": f"failure {index}",
                    "frames": [],
                }
                for index in range(member_count)
            ],
            "[limit]",
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

        expected_members = [
            {
                "error_class": "builtins.ValueError",
                "error_message": f"failure {index}",
                "frames": [],
            }
            for index in range(member_count)
        ]
        expected_members[-1]["causes"] = "[limit]"
        assert fields["exceptions"][0]["exceptions"] == [
            *expected_members,
            "[limit]",
        ]
        assert len(fields["exceptions"]) == 2
        assert fields["exceptions"][1] == "[limit]"

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
            "exceptions": [
                {
                    "error_class": "builtins.RuntimeError",
                    "error_message": "operation A failed",
                    "frames": [],
                    "causes": [
                        {
                            "error_class": "builtins.ValueError",
                            "error_message": "cause A",
                            "frames": [],
                        }
                    ],
                },
                {
                    "error_class": "builtins.RuntimeError",
                    "error_message": "operation B failed",
                    "frames": [],
                    "causes": [
                        {
                            "error_class": "builtins.ValueError",
                            "error_message": "cause B",
                            "frames": [],
                        }
                    ],
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
            "exceptions": [
                {
                    "error_class": "builtins.RuntimeError",
                    "error_message": "operation A failed",
                    "frames": [],
                    "causes": [
                        {
                            "error_class": "builtins.ValueError",
                            "error_message": "cause A",
                            "frames": [],
                        }
                    ],
                },
                {
                    "error_class": "builtins.RuntimeError",
                    "error_message": "operation B failed",
                    "frames": [],
                    "causes": [
                        {
                            "error_class": "builtins.ValueError",
                            "error_message": "cause B",
                            "frames": [],
                        }
                    ],
                },
                "[limit]",
            ],
        }


class TestConversionBoundary:
    """変換担当の結果を組み立て、原因の集約と失敗を扱う。"""

    def test_exception_extraction_leaves_sanitization_to_common_preparation(
        self,
    ) -> None:
        """例外抽出だけの入口は共通サニタイズも文字数制限も行わず、原文のフィールドを返す。"""
        message = (
            "x" * TEXT_LIMIT + " request with sk-proj-abcdef0123456789ABCDEFxyz failed"
        )
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
        assert fields["causes"][0]["error_message"] == "inner"

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
        members = fields["causes"][0]["exceptions"]
        assert members[0]["causes"][0]["error_message"] == "converted"
        assert members[1]["error_message"] == "converted"

    def test_exception_beyond_depth_limit_is_not_converted(self) -> None:
        """深さ上限を超えた例外は、指定した変換担当へ渡さない。"""
        # 外側から上限までの例外と、その一段先の例外を用意する。
        chain = [
            RuntimeError("failure") for _ in range(extraction.CAUSE_DEPTH_LIMIT + 2)
        ]
        for parent, child in zip(chain, chain[1:]):
            parent.__cause__ = child
        converter = Mock(return_value=ConvertedException(message="converted"))

        fields = extraction.extract_exception_fields(
            chain[0], exception_converter=converter
        )

        assert [call.args[0] for call in converter.call_args_list] == chain[:-1]
        for _ in range(extraction.CAUSE_DEPTH_LIMIT):
            fields = fields["causes"][0]
        assert fields["causes"] == "[limit]"

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
        assert fields["causes"][0]["causes"] == "[cycle]"


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

    def test_frame_count_at_limit_is_preserved(self) -> None:
        """frame数が上限ちょうどなら全件を抽出する。"""

        def fail(depth):
            if depth:
                fail(depth - 1)
            else:
                raise ValueError("failed")

        with pytest.raises(ValueError) as captured:
            fail(extraction.FRAME_LIMIT - 2)
        fields = extraction.extract_exception_fields(captured.value)
        assert fields is not None
        assert len(fields["frames"]) == extraction.FRAME_LIMIT
        assert fields["frames"][-1]["function"] == "fail"

    def test_frame_count_over_limit_replaces_whole_frames_field(self) -> None:
        """frame数が上限を超える場合は末尾への切り詰めも行わず、frames全体を固定マーカーにする。"""

        def fail(depth):
            if depth:
                fail(depth - 1)
            else:
                raise ValueError("failed")

        with pytest.raises(ValueError) as captured:
            fail(extraction.FRAME_LIMIT - 1)
        assert extraction.extract_exception_fields(captured.value) == {
            "error_class": "builtins.ValueError",
            "error_message": "failed",
            "frames": "[limit]",
        }
