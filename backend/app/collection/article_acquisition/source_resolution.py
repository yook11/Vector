"""受信した取得依頼の対象を、ソースの現在状態と登録定義から解決する。"""

from dataclasses import dataclass
from typing import Literal

from app.collection.article_acquisition.errors import AcquisitionSourceInvalidError
from app.collection.sources.article_source import AcquirableSource
from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.registry import acquisition_source_for
from app.collection.sources.repository import SourceRepository
from app.collection.sources.source_name import SourceName
from app.db.session import SessionFactory


@dataclass(frozen=True, slots=True)
class AcquisitionNotRequired:
    reason: Literal["inactive", "missing"]


async def resolve_acquisition_source(
    *, source_id: int, session_factory: SessionFactory
) -> AcquirableSource | AcquisitionNotRequired:
    async with session_factory() as session:
        recorded = await SourceRepository(session).get_by_id(source_id)
    if recorded is None:
        return AcquisitionNotRequired("missing")
    if not recorded.is_active:
        return AcquisitionNotRequired("inactive")
    try:
        return acquisition_source_for(SourceName(recorded.raw_name))
    except (ValueError, SourceNotRegisteredError):
        raise AcquisitionSourceInvalidError("source_not_registered") from None
