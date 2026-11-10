"""回答工程のテストで使う、回答の生成の開始・書き直しを問い合わせる口の偽物。"""

from __future__ import annotations

from app.agent.running.state import CanContinue, CannotContinue


class ScriptedAnswerGenerationGate:
    """開始と書き直しの問い合わせに渡した答えを返し、問い合わせの回数と順序を記録する。"""

    def __init__(
        self,
        *,
        start: CanContinue | CannotContinue | BaseException = CanContinue(),
        regenerations: tuple[CanContinue | CannotContinue, ...] = (),
        timeline: list[str] | None = None,
    ) -> None:
        self._start = start
        self._regenerations = list(regenerations)
        self._timeline = timeline
        self.start_calls = 0
        self.regenerate_calls = 0

    async def start(self) -> CanContinue | CannotContinue:
        self.start_calls += 1
        if self._timeline is not None:
            self._timeline.append("answer_start")
        if isinstance(self._start, BaseException):
            raise self._start
        return self._start

    async def regenerate(self) -> CanContinue | CannotContinue:
        self.regenerate_calls += 1
        if self._timeline is not None:
            self._timeline.append("regenerate")
        if not self._regenerations:
            return CanContinue()
        return self._regenerations.pop(0)
