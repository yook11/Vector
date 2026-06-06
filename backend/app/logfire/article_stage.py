"""AI 分析パイプラインの記事ステージ span helper。

curation → assessment → embedding の各 taskiq task は、taskiq の
``OpenTelemetryMiddleware`` が張る ``execute/<task_name>`` span の **子**として
``article_stage`` span を 1 つ開く。span には stage / result / article_id /
next_task_enqueued をドメイン語彙で載せ、「どの記事がどの工程をどう抜けたか」を
Logfire 上で直接クエリできるようにする。

span attribute には本文・prompt・AI response・URL query・認証情報は載せない。
低 cardinality の語彙 (stage / result / task_name) と内部 DB ID
(article_id / curation_id / analysis_id) のみを載せる。

設計方針: ステージは 3 つ (増えても 5 程度) で、特性 (result 語彙・次工程の有無・
article_id がいつ判明するか) がそれぞれ違う。共通基底に押し込めると各ステージの記録
方法がクラス間にバラけて読みにくいため、ステージごとに独立した記録口クラスと
context manager を素直に並べる。継承で共有しない。終端 embedding は
``mark_next_task_enqueued`` を持たないことで「次工程が無い」を構造で示す。

ステージ間で唯一共有するのは ``_current_stage_span`` ContextVar 1 個だけ。await 先の
deep-stack service が signature を変えずに result を書くための土管で、ステージの特性は
表さない。taskiq の async path
は ``copy_context()`` を通らず同一 await チェーンを共有するため task が積んだ handle を
service が見られる。task 間は ``asyncio.create_task`` が context をコピーするので漏れ
ない。``finally`` での ``reset(token)`` は必須。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import logfire
from logfire import LogfireSpan

# stage 別の result 語彙。値だけで「記事がどう抜けたか」が読めるよう自己記述的にする。
CurationResult = Literal["signal", "noise", "rate_limited", "skipped", "failed"]
AssessmentResult = Literal[
    "in_scope", "out_of_scope", "rate_limited", "skipped", "failed"
]
EmbeddingResult = Literal["succeeded", "rate_limited", "skipped", "failed"]

_SPAN_NAME = "article_stage"


class CurationStageSpan:
    """curation task の記録口。article_id は open 時に確定、次工程は assess_content。"""

    def __init__(self, span: LogfireSpan) -> None:
        self._span = span
        self._result_set = False

    def set_result(self, result: CurationResult) -> None:
        """result を一度だけ焼く (no-override)。"""
        if self._result_set:
            return
        self._span.set_attribute("result", result)
        self._result_set = True

    def mark_next_task_enqueued(self) -> None:
        """assess_content の kiq 成功直後に呼ぶ。enqueued フラグと次 task 名を焼く。"""
        self._span.set_attribute("next_task_enqueued", True)
        self._span.set_attribute("next_task_name", "assess_content")


class AssessmentStageSpan:
    """assessment task の記録口。article_id は ready 構築後に late-bind する。"""

    def __init__(self, span: LogfireSpan) -> None:
        self._span = span
        self._result_set = False

    def set_result(self, result: AssessmentResult) -> None:
        """result を一度だけ焼く (no-override)。"""
        if self._result_set:
            return
        self._span.set_attribute("result", result)
        self._result_set = True

    def set_article_id(self, article_id: int) -> None:
        """trigger に無く ready で判明する article_id を後付けする。"""
        self._span.set_attribute("article_id", article_id)

    def mark_next_task_enqueued(self) -> None:
        """generate_embedding の kiq 成功直後に呼ぶ。次 task 名を同時に焼く。"""
        self._span.set_attribute("next_task_enqueued", True)
        self._span.set_attribute("next_task_name", "generate_embedding")


class EmbeddingStageSpan:
    """embedding task の記録口。終端ステージなので次工程の記録手段を持たない。"""

    def __init__(self, span: LogfireSpan) -> None:
        self._span = span
        self._result_set = False

    def set_result(self, result: EmbeddingResult) -> None:
        """result を一度だけ焼く (no-override)。"""
        if self._result_set:
            return
        self._span.set_attribute("result", result)
        self._result_set = True

    def set_article_id(self, article_id: int) -> None:
        """trigger に無く ready で判明する article_id を後付けする。"""
        self._span.set_attribute("article_id", article_id)


_current_stage_span: contextvars.ContextVar[
    CurationStageSpan | AssessmentStageSpan | EmbeddingStageSpan | None
] = contextvars.ContextVar("article_stage_span", default=None)


@contextmanager
def curation_stage_span(*, article_id: int) -> Iterator[CurationStageSpan]:
    """curation task の ``article_stage`` span を開く context manager。

    open 時は ``next_task_enqueued=False`` のみ載せ、``next_task_name`` は載せない
    (kiq 成功後に ``mark_next_task_enqueued`` が同時に焼く)。例外貫通かつ result
    未設定なら backstop で ``failed`` を焼いてから例外を再送出する。
    """
    with logfire.span(
        _SPAN_NAME,
        stage="curation",
        task_name="curate_content",
        article_id=article_id,
        next_task_enqueued=False,
    ) as span:
        recorder = CurationStageSpan(span)
        token = _current_stage_span.set(recorder)
        try:
            yield recorder
        except BaseException:
            recorder.set_result("failed")
            raise
        finally:
            _current_stage_span.reset(token)


@contextmanager
def assessment_stage_span(*, curation_id: int) -> Iterator[AssessmentStageSpan]:
    """assessment task の ``article_stage`` span を開く context manager。

    article_id は trigger に無いため open 時には載せず、ready 構築後に
    ``set_article_id`` で後付けする。backstop / contextvar の扱いは curation と同じ。
    """
    with logfire.span(
        _SPAN_NAME,
        stage="assessment",
        task_name="assess_content",
        curation_id=curation_id,
        next_task_enqueued=False,
    ) as span:
        recorder = AssessmentStageSpan(span)
        token = _current_stage_span.set(recorder)
        try:
            yield recorder
        except BaseException:
            recorder.set_result("failed")
            raise
        finally:
            _current_stage_span.reset(token)


@contextmanager
def embedding_stage_span(*, analysis_id: int) -> Iterator[EmbeddingStageSpan]:
    """embedding task の ``article_stage`` span を開く context manager。

    終端ステージなので next_task 系 attribute は一切載せない。article_id は ready
    構築後に ``set_article_id`` で後付けする。backstop / contextvar は他ステージと同じ。
    """
    with logfire.span(
        _SPAN_NAME,
        stage="embedding",
        task_name="generate_embedding",
        analysis_id=analysis_id,
    ) as span:
        recorder = EmbeddingStageSpan(span)
        token = _current_stage_span.set(recorder)
        try:
            yield recorder
        except BaseException:
            recorder.set_result("failed")
            raise
        finally:
            _current_stage_span.reset(token)


def set_curation_stage_result(result: CurationResult) -> None:
    """現在の span が curation の時だけ result を焼く。それ以外は no-op。

    span 文脈外 (CLI / service 単体テスト) でも、別ステージ span の最中の誤呼び出し
    でも、recorder が curation 型でなければ何もしない (別ステージ span への誤焼き
    防止)。setter は自ステージの recorder にしか効かない。
    """
    recorder = _current_stage_span.get()
    if not isinstance(recorder, CurationStageSpan):
        return
    recorder.set_result(result)


def set_assessment_stage_result(result: AssessmentResult) -> None:
    """現在の span が assessment の時だけ result を焼く。それ以外は no-op。"""
    recorder = _current_stage_span.get()
    if not isinstance(recorder, AssessmentStageSpan):
        return
    recorder.set_result(result)


def set_embedding_stage_result(result: EmbeddingResult) -> None:
    """現在の span が embedding の時だけ result を焼く。それ以外は no-op。"""
    recorder = _current_stage_span.get()
    if not isinstance(recorder, EmbeddingStageSpan):
        return
    recorder.set_result(result)
