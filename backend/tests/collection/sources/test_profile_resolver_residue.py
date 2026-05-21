"""``CompletionProfileResolver`` adapter 削除の死亡確認 oracle。

spec ``Pending source identity refactor.md`` Chunk 4 で resolver class /
module を削除した後、コード内に名前が残らないことを git grep で機械的に
検証する。Chunk 5 以降で誤って復活させた場合の regression 検出。

検査キーワードは ``CompletionProfileResolver`` (Protocol 名) と
``RegistryCompletionProfileResolver`` (具象名) の 2 つ。``profile_resolver``
(snake_case の module 名) は史実 record として docstring に残してよい。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# ``parents[3]`` = backend/ (このファイルから 3 階層上る:
#   tests/collection/sources/test_profile_resolver_residue.py
#   → tests/collection/sources → tests/collection → tests → backend)
_BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _git_grep(pattern: str) -> subprocess.CompletedProcess[str]:
    # ``pattern`` は test 内で固定リテラル、PATH 上の ``git`` をそのまま使う
    # (CI / dev 環境で git が PATH にあることが前提)。
    return subprocess.run(  # noqa: S603
        ["git", "grep", "-n", pattern],  # noqa: S607
        cwd=_BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


_SELF_RELPATH = "tests/collection/sources/test_profile_resolver_residue.py"


def _residue_lines(stdout: str) -> list[str]:
    # この oracle 自身の docstring / 関数名は grep 対象から除外する
    # (resolver 削除完了は「他ファイルでの不在」で測る)。
    return [
        line for line in stdout.splitlines() if not line.startswith(f"{_SELF_RELPATH}:")
    ]


def test_completion_profile_resolver_class_is_gone() -> None:
    """``CompletionProfileResolver`` の名前が production / test から消えている。

    ``git grep`` 結果から自身 (oracle ファイル) を除外した残骸が空 = 0 件を
    semantic に直接 pin する (returncode 規約 (0=match / 1=no match /
    128=git error) に依存しない)。
    """
    result = _git_grep("CompletionProfileResolver")
    residue = _residue_lines(result.stdout)
    assert not residue, (
        "CompletionProfileResolver の残骸:\n"
        + "\n".join(residue)
        + f"\n(stderr: {result.stderr})"
    )


def test_registry_completion_profile_resolver_class_is_gone() -> None:
    """具象 ``RegistryCompletionProfileResolver`` も完全消滅していること。"""
    result = _git_grep("RegistryCompletionProfileResolver")
    residue = _residue_lines(result.stdout)
    assert not residue, (
        "RegistryCompletionProfileResolver の残骸:\n"
        + "\n".join(residue)
        + f"\n(stderr: {result.stderr})"
    )


def test_profile_resolver_module_is_gone() -> None:
    """``profile_resolver.py`` モジュール自体が物理削除されている。"""
    target = _BACKEND_ROOT / "app" / "collection" / "sources" / "profile_resolver.py"
    assert not target.exists(), f"profile_resolver.py が残っている: {target}"
