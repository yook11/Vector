"""OutOfScopeAssessment アグリゲート — Stage 4 で out-of-scope と判定された記録。

``OutOfScopeAssessment`` は対象範囲外判定の記録 Entity。identity (id) と
記録時刻 (rejected_at) を持つ。

``OutOfScopeAssessment`` は ``InScopeAssessment`` と別アグリゲートとして扱う:
- ``InScopeAssessment`` は「ユーザーに見せる確定評価結果」
- ``OutOfScopeAssessment`` は「監査・トレース用の対象外記録」
役割と寿命管理が違うため、実装の見た目が似ていても別型に分ける。

Stage 3 由来の ``translated_title`` / ``summary`` snapshot は in-scope と対称に
保持する。判定が成功した結果という同じ次元として扱い、Service の永続化処理に
「保存データを変える」主観判断 (= "out-of-scope は価値がない") を持ち込まない。

このアグリゲートは認証された admin ロールまたは内部 observability のみ
公開を許容する。REST API 経由で一般ユーザーに返してはならない。

AI 境界型 ``OutOfScope`` の sanitize / 長さ上限は ``classifier/schema.py`` 側で
保証されるため、Repository.save が AI 境界型をそのまま受け取り Entity を返す
(feedback_bc_boundary_guarantees_downstream)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OutOfScopeAssessment:
    """対象範囲外判定の記録 Entity。

    identity は実質 ``extraction_id`` (UNIQUE) — ``id`` は DB 都合の採番値。
    将来「extraction ごとに複数 out-of-scope 履歴」が必要になったら ``id`` が
    独立した意味を持つようになる。

    ``translated_title`` / ``summary`` は Stage 4 確定時点の Extraction からの
    スナップショット — Extraction が後に再実行されても Assessment 側は更新されない
    (in-scope と対称、out-of-scope 記録も自己完結した監査物として扱う)。

    Invariants:
    - id / extraction_id は正の整数
    - translated_title / summary / investor_take / ai_model は非空
    - rejected_at は記録時刻

    ``__post_init__`` の検査は DB CHECK + FK NOT NULL と一致する
    (DB が壊れた場合の検知用)。
    """

    id: int
    extraction_id: int
    translated_title: str
    summary: str
    investor_take: str
    ai_model: str
    rejected_at: datetime

    def __post_init__(self) -> None:
        if not self.translated_title:
            raise ValueError("OutOfScopeAssessment.translated_title must be non-empty")
        if not self.summary:
            raise ValueError("OutOfScopeAssessment.summary must be non-empty")
        if not self.investor_take:
            raise ValueError("OutOfScopeAssessment.investor_take must be non-empty")
        if not self.ai_model:
            raise ValueError("OutOfScopeAssessment.ai_model must be non-empty")
        if self.id <= 0:
            raise ValueError("OutOfScopeAssessment.id must be positive")
        if self.extraction_id <= 0:
            raise ValueError("OutOfScopeAssessment.extraction_id must be positive")
