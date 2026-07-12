"""Evidence JSONのroot answer stringだけを増分復元する。"""

from __future__ import annotations

from enum import Enum, auto

__all__ = ["IncrementalJsonAnswerExtractor"]


class _ParserState(Enum):
    START = auto()
    KEY_OR_END = auto()
    KEY = auto()
    AFTER_KEY = auto()
    VALUE = auto()
    ANSWER = auto()
    SKIP_CONTAINER = auto()
    SKIP_STRING = auto()
    SKIP_PRIMITIVE = auto()
    AFTER_VALUE = auto()
    DONE = auto()
    DISABLED = auto()


class _JsonStringDecoder:
    def __init__(self) -> None:
        self._escape = False
        self._unicode_digits: str | None = None
        self._pending_high_surrogate: int | None = None
        self._invalid = False

    @property
    def can_close(self) -> bool:
        return not self._escape and self._unicode_digits is None

    @property
    def invalid(self) -> bool:
        return self._invalid

    def append(self, character: str) -> str:
        if self._unicode_digits is not None:
            if character not in "0123456789abcdefABCDEF":
                self._unicode_digits = None
                self._pending_high_surrogate = None
                self._invalid = True
                return ""
            self._unicode_digits += character
            if len(self._unicode_digits) < 4:
                return ""
            code_unit = int(self._unicode_digits, 16)
            self._unicode_digits = None
            return self._decode_code_unit(code_unit)

        if self._escape:
            self._escape = False
            if character == "u":
                self._unicode_digits = ""
                return ""
            decoded = {
                '"': '"',
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }.get(character)
            self._pending_high_surrogate = None
            if decoded is None:
                self._invalid = True
            return decoded or ""

        if character == "\\":
            self._escape = True
            return ""
        if ord(character) < 0x20:
            self._pending_high_surrogate = None
            self._invalid = True
            return ""
        self._pending_high_surrogate = None
        return character

    def finish(self) -> None:
        self._escape = False
        self._unicode_digits = None
        self._pending_high_surrogate = None

    def _decode_code_unit(self, code_unit: int) -> str:
        pending_high = self._pending_high_surrogate
        if pending_high is not None:
            self._pending_high_surrogate = None
            if 0xDC00 <= code_unit <= 0xDFFF:
                code_point = (
                    0x10000 + ((pending_high - 0xD800) << 10) + (code_unit - 0xDC00)
                )
                return chr(code_point)

        if 0xD800 <= code_unit <= 0xDBFF:
            self._pending_high_surrogate = code_unit
            return ""
        if 0xDC00 <= code_unit <= 0xDFFF:
            return ""
        return chr(code_unit)


class IncrementalJsonAnswerExtractor:
    """root object直下のstring型answerだけをdecodeする。"""

    def __init__(self) -> None:
        self._state = _ParserState.START
        self._string_decoder: _JsonStringDecoder | None = None
        self._key = ""
        self._current_key = ""
        self._seen_keys: set[str] = set()
        self._container_stack: list[str] = []
        self._skip_in_string = False
        self._skip_escape = False
        self._finished = False

    def append(self, raw_fragment: str) -> str:
        """新たに確定したanswer文字列だけを返す。"""
        if self._finished:
            raise RuntimeError("finished extractor cannot accept more JSON")

        answer: list[str] = []
        for character in raw_fragment:
            self._consume(character, answer)
        return "".join(answer)

    def finish(self) -> str:
        """未完成escapeやsurrogateを破棄して終了する。"""
        if self._finished:
            return ""
        if self._string_decoder is not None:
            self._string_decoder.finish()
        self._finished = True
        return ""

    def _consume(self, character: str, answer: list[str]) -> None:
        if self._state in (_ParserState.DONE, _ParserState.DISABLED):
            return

        if self._state is _ParserState.START:
            if character.isspace():
                return
            self._state = (
                _ParserState.KEY_OR_END if character == "{" else _ParserState.DISABLED
            )
            return

        if self._state is _ParserState.KEY_OR_END:
            if character.isspace():
                return
            if character == "}":
                self._state = _ParserState.DONE
                return
            if character != '"':
                self._state = _ParserState.DISABLED
                return
            self._key = ""
            self._string_decoder = _JsonStringDecoder()
            self._state = _ParserState.KEY
            return

        if self._state is _ParserState.KEY:
            decoder = self._string_decoder
            if decoder is None:
                self._state = _ParserState.DISABLED
                return
            if character == '"' and decoder.can_close:
                decoder.finish()
                self._string_decoder = None
                if self._key in self._seen_keys:
                    self._state = _ParserState.DISABLED
                    return
                self._seen_keys.add(self._key)
                self._current_key = self._key
                self._state = _ParserState.AFTER_KEY
                return
            self._key += decoder.append(character)
            if decoder.invalid:
                self._state = _ParserState.DISABLED
            return

        if self._state is _ParserState.AFTER_KEY:
            if character.isspace():
                return
            self._state = (
                _ParserState.VALUE if character == ":" else _ParserState.DISABLED
            )
            return

        if self._state is _ParserState.VALUE:
            if character.isspace():
                return
            if self._current_key == "answer" and character == '"':
                self._string_decoder = _JsonStringDecoder()
                self._state = _ParserState.ANSWER
                return
            self._start_skipped_value(character)
            return

        if self._state is _ParserState.ANSWER:
            decoder = self._string_decoder
            if decoder is None:
                self._state = _ParserState.DISABLED
                return
            if character == '"' and decoder.can_close:
                decoder.finish()
                self._string_decoder = None
                self._state = _ParserState.AFTER_VALUE
                return
            decoded = decoder.append(character)
            if decoder.invalid:
                self._state = _ParserState.DISABLED
                return
            if decoded:
                answer.append(decoded)
            return

        if self._state is _ParserState.SKIP_CONTAINER:
            self._consume_skipped_container(character)
            return

        if self._state is _ParserState.SKIP_STRING:
            if self._skip_escape:
                self._skip_escape = False
            elif character == "\\":
                self._skip_escape = True
            elif character == '"':
                self._state = _ParserState.AFTER_VALUE
            return

        if self._state is _ParserState.SKIP_PRIMITIVE:
            if character == ",":
                self._state = _ParserState.KEY_OR_END
            elif character == "}":
                self._state = _ParserState.DONE
            return

        if self._state is _ParserState.AFTER_VALUE:
            if character.isspace():
                return
            if character == ",":
                self._state = _ParserState.KEY_OR_END
            elif character == "}":
                self._state = _ParserState.DONE
            else:
                self._state = _ParserState.DISABLED
            return

    def _start_skipped_value(self, character: str) -> None:
        if character == "{":
            self._container_stack = ["}"]
            self._state = _ParserState.SKIP_CONTAINER
        elif character == "[":
            self._container_stack = ["]"]
            self._state = _ParserState.SKIP_CONTAINER
        elif character == '"':
            self._skip_escape = False
            self._state = _ParserState.SKIP_STRING
        else:
            self._state = _ParserState.SKIP_PRIMITIVE

    def _consume_skipped_container(self, character: str) -> None:
        if self._skip_in_string:
            if self._skip_escape:
                self._skip_escape = False
            elif character == "\\":
                self._skip_escape = True
            elif character == '"':
                self._skip_in_string = False
            return

        if character == '"':
            self._skip_in_string = True
            return
        if character == "{":
            self._container_stack.append("}")
            return
        if character == "[":
            self._container_stack.append("]")
            return
        if self._container_stack and character == self._container_stack[-1]:
            self._container_stack.pop()
            if not self._container_stack:
                self._state = _ParserState.AFTER_VALUE
