"""ReadyForArticleCompletion — Stage 2 実行可能状態の precondition 型 (厚い Ready)。

Stage 2 (Pattern H: ``IncompleteArticle`` → ``AnalyzableArticle`` 補完) を実行して
よい状態を構造保証し、かつ補完処理に必要な値 (``incomplete_article`` /
stale worker guard 用 ``attempt_count``) を全揃えで運ぶ厚い Ready。

設計方針 (2026-05-11 確定、案 3。memory ``project_typed_pipeline_preconditions``):
Ready 型は **処理に必要な値の全揃え** を構造保証する厚い型であり、**下流 Stage
自身 (Stage 2 ``extract_html_body`` Task) が処理開始時に DB から構築** する。
上流 (cron dispatcher) から Stage 2 への kiq message は ``pending_id`` のみ運び、
Task が ``ReadyForArticleCompletion.try_advance_from`` を呼んで最新の DB 状態から
Ready を構築する。Stage 3 ``ReadyForExtraction`` / Stage 4 ``ReadyForAssessment``
と完全同型 (`try_advance_from` は Repository の ``try_load_*`` への thin delegate)。

``@dataclass(frozen=True, slots=True)`` を使う理由 (AI 側 ``ReadyForExtraction``
が ``BaseModel`` なのと異なる): 本型は kiq payload にならない。Task は
``pending_id: int`` を受け取り in-process で Ready を構築して
``ArticleCompletionService.execute(ready)`` に直接渡すため、taskiq formatter の
Pydantic BaseModel 要求 (memory ``feedback_taskiq_basemodel_required``) は非該当。
``status='running'`` precondition は repository query が構造保証する
(memory ``feedback_structural_guarantee``)。``IncompleteArticle`` (Pydantic) を
内包する frozen dataclass という passport 形状を維持する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.collection.domain.incomplete_article import IncompleteArticle


class ArticleCompletionPreconditionProtocol(Protocol):
    """Stage 2 進行判定用 Article Completion Repository contract。

    「Ready 構築に必要なデータをロードする」=「ReadyForArticleCompletion を
    満たす」という意味論で、Repository は precondition (``status='running'``)
    を満たす場合に ``ReadyForArticleCompletion`` を構築して返す責務を持つ。
    ``try_advance_from`` は本 Protocol への thin delegate (Stage 3
    ``ExtractionPreconditionProtocol`` と同型)。
    """

    async def try_load_for_completion(
        self, pending_id: int
    ) -> ReadyForArticleCompletion | None: ...


@dataclass(frozen=True, slots=True)
class ReadyForArticleCompletion:
    """Stage 2 補完を実行可能な状態を表す precondition 型 (厚い Ready)。

    この型が作られるのは ``status='running'`` の pending 行だけ。``status`` /
    ``ready_at`` / ``leased_until`` は処理資格判定後の service には不要なので
    持たせない。``attempt_count`` は retry 予算判定と stale worker guard の SSoT。
    """

    pending_id: int
    source_id: int
    attempt_count: int
    incomplete_article: IncompleteArticle

    @classmethod
    async def try_advance_from(
        cls,
        *,
        pending_id: int,
        repo: ArticleCompletionPreconditionProtocol,
    ) -> ReadyForArticleCompletion | None:
        """pending_id から Stage 2 へ advance できるかを判定する gatekeeper。

        Precondition (Stage 2 に進める条件): 同 pending_id の
        ``pending_html_articles`` 行が ``status='running'`` (cron dispatcher が
        claim 済)。未 claim / sweep 済 / close 済 / delete 済はすべて進めない。

        本 method は Domain 層の named gateway として
        ``Repository.try_load_for_completion`` にそのまま delegate する
        (Stage 3 ``ReadyForExtraction.try_advance_from`` と同型)。

        Returns:
            進める場合: ``ReadyForArticleCompletion`` (補完入力値を含む厚い型)
            進めない場合: ``None`` (業務正常状態、例外ではない)

        Args:
            pending_id: cron dispatcher が claim した
                ``pending_html_articles.id``
            repo: ``try_load_for_completion`` を備える Repository
        """
        return await repo.try_load_for_completion(pending_id)
