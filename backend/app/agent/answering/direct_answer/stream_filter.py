"""Direct answer の表示用増分テキストを生成する。"""

from __future__ import annotations

from enum import Enum, auto

__all__ = ["DirectAnswerVisibleTextFilter"]


class _MarkerState(Enum):
    TEXT = auto()
    OPEN = auto()
    DOUBLE_OPEN = auto()
    DIGITS = auto()
    CLOSE = auto()


class DirectAnswerVisibleTextFilter:
    """citation marker と外側の空白を増分入力から除く。"""

    def __init__(self) -> None:
        self._marker_state = _MarkerState.TEXT
        self._marker_candidate = ""
        self._pending_whitespace = ""
        self._has_visible_text = False
        self._finished = False

    def append(self, text: str) -> str:
        """確定した表示用断片を返す。"""
        if self._finished:
            raise RuntimeError("finished filter cannot accept more text")

        visible: list[str] = []
        index = 0
        while index < len(text):
            character = text[index]

            if self._marker_state is _MarkerState.TEXT:
                if character == "[":
                    self._marker_candidate = character
                    self._marker_state = _MarkerState.OPEN
                else:
                    self._append_literal(character, visible)
                index += 1
                continue

            if self._marker_state is _MarkerState.OPEN:
                if character == "[":
                    self._marker_candidate += character
                    self._marker_state = _MarkerState.DOUBLE_OPEN
                    index += 1
                else:
                    self._append_literal(self._marker_candidate, visible)
                    self._reset_marker()
                continue

            if self._marker_state is _MarkerState.DOUBLE_OPEN:
                if _is_ascii_digit(character):
                    self._marker_candidate += character
                    self._marker_state = _MarkerState.DIGITS
                    index += 1
                else:
                    self._append_literal("[", visible)
                    self._marker_candidate = "["
                    self._marker_state = _MarkerState.OPEN
                continue

            if self._marker_state is _MarkerState.DIGITS:
                if _is_ascii_digit(character):
                    self._marker_candidate += character
                    index += 1
                elif character == "]":
                    self._marker_candidate += character
                    self._marker_state = _MarkerState.CLOSE
                    index += 1
                else:
                    self._append_literal(self._marker_candidate, visible)
                    self._reset_marker()
                continue

            if character == "]":
                self._reset_marker()
                index += 1
            else:
                self._append_literal(self._marker_candidate, visible)
                self._reset_marker()

        return "".join(visible)

    def finish(self) -> str:
        """未完成markerをliteralとして確定し、末尾空白を捨てる。"""
        if self._finished:
            return ""

        visible: list[str] = []
        if self._marker_candidate:
            self._append_literal(self._marker_candidate, visible)
            self._reset_marker()
        self._pending_whitespace = ""
        self._finished = True
        return "".join(visible)

    def _append_literal(self, text: str, visible: list[str]) -> None:
        for character in text:
            if character.isspace():
                if self._has_visible_text:
                    self._pending_whitespace += character
                continue

            if self._pending_whitespace:
                visible.append(self._pending_whitespace)
                self._pending_whitespace = ""
            visible.append(character)
            self._has_visible_text = True

    def _reset_marker(self) -> None:
        self._marker_candidate = ""
        self._marker_state = _MarkerState.TEXT


def _is_ascii_digit(character: str) -> bool:
    return "0" <= character <= "9"
