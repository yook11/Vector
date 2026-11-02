"""例外の種類に依存しない変換結果と変換関数の契約。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

type ErrorDetails = Mapping[str, object]


@dataclass(frozen=True)
class ConvertedException:
    """型ごとの変換が決めた原因文・診断情報と、内側の例外を出すかを抽出へ渡す。"""

    error_message: str
    error_details: ErrorDetails | None = None
    # 内側の例外を安全に出せない型は、抽出にこの例外より先をたどらせない。
    omit_inner_exceptions: bool = False


type ExceptionConverter = Callable[[BaseException], ConvertedException]
