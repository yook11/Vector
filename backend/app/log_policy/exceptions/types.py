"""例外の種類に依存しない変換結果と変換関数の契約。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

type ErrorDetails = Mapping[str, object]


@dataclass(frozen=True)
class ConvertedException:
    """各変換が生成した原因文と固有の診断情報を共通処理へ渡す。"""

    message: str
    error_details: ErrorDetails | None = None
    cause_is_aggregated: bool = False


type ExceptionConverter = Callable[[BaseException], ConvertedException]
