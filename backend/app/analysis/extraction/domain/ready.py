"""ReadyForExtraction — Stage C 実行可能状態の precondition 型 (Pattern A')。

spec `specs/typed-pipeline-preconditions.md` §1.1 / §3.2 / §6.1 / §7 で確定した設計
の extraction BC 実装。Stage C operation の前提条件 (Article 存在 + Extraction
未生成 + 本文サイズが system hard cap 以内) を構造保証し、ExtractionService の
precondition 分岐 (冪等ヒット / article_not_found) を消すために Stage 間 passport
として受け渡される。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
詳細は memory `feedback_taskiq_basemodel_required.md`。

`MAX_CONTENT_LENGTH` は system 不変条件としての hard cap (リソース保護) であり、
adapter 固有の入力整形 (例: GeminiExtractionPrompt.CONTENT_MAX_LENGTH = 20_000) と
責務が異なる。前者は「ここを超える本文は Stage C の対象外」を表し、後者は
「特定モデルにこのサイズで投げる」を表す。
"""

from __future__ import annotations

from typing import ClassVar, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)


class ExtractionExistenceProtocol(Protocol):
    """Stage C 進行判定用 ExtractionRepository contract (cheap exists 判定)。

    Stage 1 signal/noise フィルタの導入により、同一 article に対して
    ``article_extractions`` または ``extraction_noises`` のどちらかが既に
    存在する状態を Ready 型構築時に弾く必要がある。Stage 3 永続化層は
    ``ExtractionRepository`` 1 クラスに集約されているため、本 Protocol は
    signal / noise 両 exists 判定をまとめて提供する。"""

    async def signal_exists_for_article(self, article_id: int) -> bool: ...

    async def noise_exists_for_article(self, article_id: int) -> bool: ...


class ReadyForExtraction(BaseModel):
    """Stage C extraction を実行可能な状態を表す precondition 型。

    フィールドは operation に必要な値だけ (article_id + extractor に渡す
    タイトル / 本文)。

    Invariants:
    - ``article_id``: 正の整数 (DB の Article.id を指す)
    - ``original_title``: 非空 (構築時 ``Field(min_length=1)`` で保証)
    - ``original_content``: 非空かつ ``MAX_CONTENT_LENGTH`` 以内
      (構築時 ``Field(min_length=1, max_length=...)`` で保証)
    - frozen: 生成後は不変 (Stage 間 passport として副作用なしに受け渡せる)
    """

    model_config = ConfigDict(frozen=True)

    MAX_CONTENT_LENGTH: ClassVar[int] = 200_000

    article_id: int = Field(gt=0)
    original_title: str = Field(min_length=1)
    original_content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)

    @classmethod
    async def try_advance_from(
        cls,
        *,
        article_id: int,
        original_title: str,
        original_content: str,
        extraction_repo: ExtractionExistenceProtocol,
    ) -> ReadyForExtraction | None:
        """Article 永続化から Stage C へ advance できるかを判定する gatekeeper。

        Precondition (Stage C に進める条件):
        - 同 article_id の Extraction 未生成
        - 同 article_id の ExtractionNoise 未生成 (Stage 1 で既に noise 判定済の
          記事を再処理しない)
        - 本文長が ``MAX_CONTENT_LENGTH`` 以内 (system hard cap)

        Phase 3 は BC 越境 (collection BC → analysis BC) を含み、上流 caller が
        持つ Article 型が複数 (collection 域 Entity / ORM Article) になりうるため、
        signature は型に依存しない data 値の kwargs を採用する。Phase 1/2 では
        単一 BC 内のため source Entity 1 引数で良かった。

        signal / noise 両 exists 判定は ``ExtractionRepository`` 1 つで賄える
        (Stage 4 ``AssessmentRepository`` と同型)。

        Returns:
            進める場合: `ReadyForExtraction`
            進めない場合: `None` (業務正常状態、例外ではない — spec §4.5 Failure mode 1)
        """
        if await extraction_repo.signal_exists_for_article(article_id):
            return None
        if await extraction_repo.noise_exists_for_article(article_id):
            return None
        if len(original_content) > cls.MAX_CONTENT_LENGTH:
            logger.warning(
                "extraction_skipped_oversized_article",
                article_id=article_id,
                content_length=len(original_content),
                max_length=cls.MAX_CONTENT_LENGTH,
            )
            return None
        return cls(
            article_id=article_id,
            original_title=original_title,
            original_content=original_content,
        )
