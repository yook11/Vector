"""worker がログに出す、Run を続けられない理由の名前(DB を使わない)。"""

from __future__ import annotations

import pytest

from app.agent.run_execution import _stop_reason
from app.agent.running import state
from app.agent.running.state import CannotContinue

# slots付きdataclassは作り直す前のクラスも__subclasses__に残すので、公開名から集める。
_REASON_TYPES = [
    exported
    for exported in (getattr(state, name) for name in state.__all__)
    if isinstance(exported, type)
    and issubclass(exported, CannotContinue)
    and exported is not CannotContinue
]


@pytest.mark.parametrize("reason_type", _REASON_TYPES, ids=lambda cls: cls.__name__)
def test_every_stop_reason_has_its_own_log_name(
    reason_type: type[CannotContinue],
) -> None:
    """続けられない理由の型は、どれもログに出す名前を持つ。"""
    assert _stop_reason(reason_type())


def test_stop_reason_log_names_are_distinct() -> None:
    """続けられない理由の型ごとに、ログに出す名前が重ならない。"""
    names = {_stop_reason(reason_type()) for reason_type in _REASON_TYPES}
    assert len(names) == len(_REASON_TYPES)
