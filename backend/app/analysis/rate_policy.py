"""AI provider rate limit policy — provider/model 単位のキー組み立て VO。

Gemini 公式は rate limit を project × model で適用するため、アプリ側のキー
名前空間も provider × model で揃える。stage (extract/assess/embed) が同一
モデルを共有する場合でも 1 つのカウンタを共有することで、provider 実 quota
と整合した予算管理になる。

入力契約は ``RatePolicySource`` Protocol で duck-typed に表現する (PROVIDER /
MODEL / RPM / RPD を ClassVar で備える AI component)。``from_component()`` は
duck-typed なので runtime 引数は ``object`` を受け取り、``__post_init__`` の
型 + 値 validation で MagicMock の silent 漏れ等を弾く。Protocol は呼び出し側
に期待する形を伝える「契約のドキュメント」として残し、type-checker による
強制は ``__post_init__`` の構造 guard に委ねる。Base 抽象クラス側の必須 ClassVar
化 (PROVIDER 含む) は後続 PR (Spec 分離) で行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol, Self


class RatePolicySource(Protocol):
    """``RatePolicy`` を構築するために必要な AI 呼び出しメタデータ。

    duck-typed: concrete AI class が以下 4 つの ClassVar を備えていれば
    ``RatePolicy.from_component`` に渡せる。Base 抽象クラスを継承していなくても
    構造的に満たせばよい。Base クラス側は ``MODEL`` 等を ``ClassVar`` で宣言
    しているため、Protocol 側も ``ClassVar`` で揃える。
    """

    PROVIDER: ClassVar[str]
    MODEL: ClassVar[str]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]


@dataclass(frozen=True, slots=True)
class RatePolicy:
    """provider × model 粒度の rate limit 設定値オブジェクト。

    Redis キーは ``ratelimit:{provider}:{model}:{rpm|rpd}`` に固定する。
    同一 provider 同一 model なら、呼び出し元 stage (extract/assess/embed) が
    違ってもキーは共有される (provider 側の実 quota と整合)。
    """

    provider: str
    model: str
    rpm: int | None
    rpd: int | None

    @classmethod
    def from_component(cls, component: object) -> Self:
        """AI component の ClassVar から policy を構築する。

        引数は ``RatePolicySource`` 構造 (PROVIDER / MODEL / RPM / RPD ClassVar)
        を期待するが、Base 抽象クラス側で PROVIDER がまだ宣言されていない
        移行期 (PR4 で整理) のため duck-typed で ``object`` を受ける。属性欠落 /
        型不正は ``__post_init__`` で ``ValueError`` に詰めて即座に拒否する。
        """
        return cls(
            provider=getattr(component, "PROVIDER", None),
            model=getattr(component, "MODEL", None),
            rpm=getattr(component, "RPM", None),
            rpd=getattr(component, "RPD", None),
        )

    @property
    def rpm_key(self) -> str:
        return f"ratelimit:{self.provider}:{self.model}:rpm"

    @property
    def rpd_key(self) -> str:
        return f"ratelimit:{self.provider}:{self.model}:rpd"

    def __post_init__(self) -> None:
        # duck typing の弱点 (MagicMock が未定義属性でも MagicMock を返す等) を
        # 構造的に塞ぐ。``ratelimit:<MagicMock...>:model:rpm`` のような silent
        # 異常キー生成を test/runtime いずれでも即座に弾く。
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError(f"provider must be non-empty str, got {self.provider!r}")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError(f"model must be non-empty str, got {self.model!r}")
        if self.rpm is not None and (not isinstance(self.rpm, int) or self.rpm <= 0):
            raise ValueError(f"rpm must be None or positive int, got {self.rpm!r}")
        if self.rpd is not None and (not isinstance(self.rpd, int) or self.rpd <= 0):
            raise ValueError(f"rpd must be None or positive int, got {self.rpd!r}")
