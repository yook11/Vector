"""article acquisition の Logfire metric (変換ファネル / run 信頼性)。

acquisition の「成功率」は 2 指標に分かれる:

- entry レベル (``vector.acquisition.outcome``): 取得した記事 1 件が analyzable /
  observed / rejected のどれになったか。observed は補完待ち成功で、監査の
  ``SUCCEEDED/incomplete_article_created`` と一致する (= acquisition では成功)。
- run レベル (``vector.acquisition.run``): source に到達して取得を完走できたか
  (succeeded / failed)。pipeline_events は run 成功 heartbeat を持たないため、run
  成功率の分母はこの counter が埋める。

attributes は cardinality 爆発を避けるため closed 語彙の ``result`` のみに保ち、
source / 棄却理由 / 例外詳細は監査 (pipeline_events) に寄せる (dispatch metric と
同方針)。
"""

from __future__ import annotations

from enum import StrEnum

import logfire


class AcquisitionEntryOutcome(StrEnum):
    """``vector.acquisition.outcome`` の result 属性 (entry 変換の結末)。"""

    ANALYZABLE = "analyzable"
    OBSERVED = "observed"
    REJECTED = "rejected"


class AcquisitionRunResult(StrEnum):
    """``vector.acquisition.run`` の result 属性 (source 取得 run の結末)。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


_outcome_counter = logfire.metric_counter(
    "vector.acquisition.outcome",
    unit="1",
    description="acquisition entry 変換の結末件数 (analyzable/observed/rejected 別)",
)
_run_counter = logfire.metric_counter(
    "vector.acquisition.run",
    unit="1",
    description="acquisition source 取得 run の結末件数 (succeeded/failed 別)",
)


def record_acquisition_outcome(outcome: AcquisitionEntryOutcome, *, count: int) -> None:
    """entry 変換の結末を ``count`` 件 counter に加算する (0 は弾く)。"""
    if count:
        _outcome_counter.add(count, attributes={"result": outcome.value})


def record_acquisition_run(result: AcquisitionRunResult) -> None:
    """source 取得 run の結末を 1 件 counter に加算する。"""
    _run_counter.add(1, attributes={"result": result.value})
