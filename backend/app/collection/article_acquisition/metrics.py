"""article acquisition の Logfire metric (変換ファネル)。

``vector.acquisition.outcome`` は取得した記事 1 件が analyzable / observed /
rejected のどれになったかを数える。observed は補完待ち成功で、監査の
``SUCCEEDED/incomplete_article_created`` と一致する (= acquisition では成功)。

attributes は cardinality 爆発を避けるため closed 語彙の ``result`` のみに保ち、
source / 棄却理由 / 例外詳細は監査 (pipeline_events) に寄せる。
"""

from __future__ import annotations

from enum import StrEnum

import logfire


class AcquisitionEntryOutcome(StrEnum):
    """``vector.acquisition.outcome`` の result 属性 (entry 変換の結末)。"""

    ANALYZABLE = "analyzable"
    OBSERVED = "observed"
    REJECTED = "rejected"


_outcome_counter = logfire.metric_counter(
    "vector.acquisition.outcome",
    unit="1",
    description="acquisition entry 変換の結末件数 (analyzable/observed/rejected 別)",
)


def record_acquisition_outcome(outcome: AcquisitionEntryOutcome, *, count: int) -> None:
    """entry 変換の結末を ``count`` 件 counter に加算する (0 は弾く)。"""
    if count:
        _outcome_counter.add(count, attributes={"result": outcome.value})
