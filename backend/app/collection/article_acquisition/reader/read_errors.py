"""応答は受け取ったが reader が構造化できなかった read-domain origin error。

接続境界の ``external_fetch_errors.py`` と対称な「読取」語彙の SSoT。本 module は
「何が起きたか (reason)」と安全文脈 (response_format / field / parser_position) を
扱い、retry 可否 / scheduling / action は段が持つ (読取失敗は実質すべて terminal
なので marker 側で ``NON_RETRYABLE`` 固定。retryable 属性は持たない)。

各 reason の value はそのまま audit ``outcome_code`` に焼かれる (``acquisition_
conversion`` の ``AcquisitionConversionDefect`` と同じ「値 = コード」パターン)。
``_default_message`` は PII-free: reason / response_format / field / parser_position
の安全値のみを合成し、上流の生バイト・生値は一切載せない (raw を渡せる引数が
constructor に存在しないのが第一の構造保証)。
"""

from __future__ import annotations

from enum import StrEnum


class UnreadableResponseReason(StrEnum):
    """読取失敗の原因 (自己記述コード)。value がそのまま ``outcome_code``。

    parse / structure は説明上のグループにすぎず (型でも属性でもない)、分類は
    この 4 reason が担う。format (xml/json/feed) は CODE に焼かず安全文脈に持つ
    (ソースから逆引き可。fetch がアダプタを CODE に入れないのと同型)。
    """

    # parse 系: body そのものを構造化する手前で落ちた。
    EMPTY_BODY = "read_empty_body"
    MALFORMED_CONTENT = "read_malformed_content"
    # structure 系: 構文解析は通ったが入れ物 / フィールドの形が想定外。
    UNEXPECTED_ROOT_SHAPE = "read_unexpected_root_shape"
    UNEXPECTED_FIELD_SHAPE = "read_unexpected_field_shape"


class UnreadableResponseError(Exception):
    """取得済み payload を reader が構造化できなかった read-domain origin error。

    ``ExternalFetchError`` family と対称の origin error (``VectorDomainError`` は
    継承しない)。``reason`` (何が起きたか) と安全文脈を instance に持ち、``CODE`` は
    reason.value を公開する (marker base が origin の ``CODE`` を outcome_code に
    焼く配線をそのまま使う)。``__str__`` は明示 message があればそれ、無ければ
    PII-free な ``_default_message`` を返す (fetch family と対称)。
    """

    def __init__(
        self,
        message: str = "",
        *,
        reason: UnreadableResponseReason,
        response_format: str,
        field: str | None = None,
        parser_position: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.response_format = response_format
        self.field = field
        self.parser_position = parser_position

    @property
    def CODE(self) -> str:  # noqa: N802 (fetch family の ClassVar ``CODE`` と同名 API)
        """reason.value を outcome_code として公開する (per-instance)。"""
        return self.reason.value

    def __str__(self) -> str:
        explicit = super().__str__()
        return explicit if explicit else self._default_message()

    def _default_message(self) -> str:
        """PII-free な既定 message (reason / format / field / position のみ合成)。"""
        parts = [f"{self.reason.value}: {self.response_format}"]
        if self.field is not None:
            parts.append(f"field={self.field}")
        if self.parser_position is not None:
            parts.append(f"at={self.parser_position}")
        return " ".join(parts)
