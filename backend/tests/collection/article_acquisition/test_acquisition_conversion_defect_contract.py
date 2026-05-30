"""``AcquisitionConversionDefect`` の audit outcome_code 契約テスト。

domain ``test_analyzable_article_defect_code_contract.py`` と同形: enum.value が
そのまま audit に焼かれる自己記述コードであることを構造的に保証する。acquisition
固有の棄却理由 (title 欠落 / 想定外バグ) は acquisition がスコープ所有するため
prefix は ``acquisition_conversion_``。語順例外メンバーは無い。
"""

from __future__ import annotations

import pytest

from app.collection.article_acquisition.errors import AcquisitionConversionDefect


@pytest.mark.parametrize("member", list(AcquisitionConversionDefect))
def test_defect_code_value_is_audit_outcome_code(
    member: AcquisitionConversionDefect,
) -> None:
    assert member.value == f"acquisition_conversion_{member.name.lower()}"
