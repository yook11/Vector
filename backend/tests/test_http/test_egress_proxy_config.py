"""Squid設定テンプレートの非公開IP拒否ACLと評価順序を検証する。"""

from pathlib import Path

import pytest

_SQUID_CONF_TEMPLATE = (
    Path(__file__).parents[3] / "infra" / "aws" / "templates" / "squid.conf.tftpl"
)
_DENY_NON_PUBLIC = "http_access deny to_private"


def _squid_directives() -> list[str]:
    """コメントと Terraform の制御行を落とした Squid ディレクティブ列 (評価順)。"""
    return [
        stripped
        for line in _SQUID_CONF_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if (stripped := line.strip()) and not stripped.startswith(("#", "%{"))
    ]


class TestEgressProxyDenyContract:
    """実プロキシの稼働状態とは別に、テンプレート上の拒否設定を保証する。"""

    def test_template_is_readable(self) -> None:
        """正本の場所がずれたら黙って緑にならず、ここで落ちる。"""
        assert _SQUID_CONF_TEMPLATE.is_file()

    def test_denies_non_public_destinations(self) -> None:
        assert _DENY_NON_PUBLIC in _squid_directives()

    @pytest.mark.parametrize("variable", ["private_v4_ranges", "private_v6_ranges"])
    def test_deny_covers_range_source(self, variable: str) -> None:
        """acl が正本の v4 / v6 双方を参照する (片方の列挙漏れは穴になる)。"""
        acl = " ".join(
            d for d in _squid_directives() if d.startswith("acl to_private ")
        )
        assert variable in acl

    def test_deny_precedes_every_allow(self) -> None:
        """Squid は上から評価するので、allow より後ろに置いた deny は死ぬ。"""
        directives = _squid_directives()
        first_allow = next(
            i for i, d in enumerate(directives) if d.startswith("http_access allow")
        )
        assert directives.index(_DENY_NON_PUBLIC) < first_allow
