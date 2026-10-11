"""取得した記事ページの応答を、抽出処理へ渡す形で保持する。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RawResponse:
    """取得済みの応答をHTTPクライアントに依存せず保持する。"""

    url: str
    content_type: str | None
    charset_from_header: str | None
    content: bytes
    decoded_text: str
