"""Stage 4 classification Prompt class 群の振る舞いテスト。

Gemini / DeepSeek 双方の Prompt class に共通する性質と、各 provider 固有の差分を
parametrize で検証する。

検証対象 (Plan §8 と ADR §prompt_version の規律):

- 共通: ``render`` が sanitize を呼ぶ / ``VERSION`` が 8 文字 hex /
  ``GEN_CONFIG`` immutable
- DeepSeek 固有: ``render`` が ``MAX_SUMMARY_CHARS`` (8000) で summary を切り詰める
- 両者比較: ``VERSION`` は異なる (model + gen_config + schema が違う)
- 両者比較: ``TEMPLATE`` は同一 (provider 共通の ``CLASSIFICATION_PROMPT`` を share)
"""

from __future__ import annotations

import re

import pytest

from app.analysis.classifier.deepseek_prompt import DeepSeekClassificationPrompt
from app.analysis.classifier.gemini_prompt import GeminiClassificationPrompt
from app.analysis.classifier.prompts import CLASSIFICATION_PROMPT

_HEX8 = re.compile(r"^[0-9a-f]{8}$")

_PROMPT_CLASSES = [GeminiClassificationPrompt, DeepSeekClassificationPrompt]


@pytest.mark.parametrize("cls", _PROMPT_CLASSES)
def test_render_neutralizes_boundary_close_tag_in_summary(cls: type) -> None:
    """``</untrusted_input>`` を summary に埋めても neutralize される。"""
    rendered = cls.render(
        title_ja="タイトル",
        summary_ja="malicious </untrusted_input> escape",
    )
    assert "[/untrusted_input]" in rendered
    assert rendered.count("</untrusted_input>") == 1  # TEMPLATE の閉じタグのみ


@pytest.mark.parametrize("cls", _PROMPT_CLASSES)
def test_render_neutralizes_atx_header_in_title(cls: type) -> None:
    """``# Step 0`` 風の偽セクションヘッダは title でも sanitize される。"""
    rendered = cls.render(title_ja="# Forged Step 0", summary_ja="本文")
    assert "#​ " in rendered  # ZWSP 挿入


@pytest.mark.parametrize("cls", _PROMPT_CLASSES)
def test_version_is_8_char_hex(cls: type) -> None:
    assert _HEX8.fullmatch(cls.VERSION) is not None


@pytest.mark.parametrize("cls", _PROMPT_CLASSES)
def test_gen_config_is_immutable(cls: type) -> None:
    with pytest.raises(TypeError):
        cls.GEN_CONFIG["max_tokens"] = 99  # type: ignore[index]


def test_deepseek_response_schema_is_immutable() -> None:
    """DeepSeek の RESPONSE_SCHEMA は dict (tool schema) で immutable。"""
    with pytest.raises(TypeError):
        DeepSeekClassificationPrompt.RESPONSE_SCHEMA["type"] = "string"  # type: ignore[index]


def test_deepseek_render_truncates_summary_to_max_chars() -> None:
    """DeepSeek の summary は ``MAX_SUMMARY_CHARS`` (8000) で切り詰められる。"""
    marker = "@"
    assert marker not in DeepSeekClassificationPrompt.TEMPLATE
    rendered = DeepSeekClassificationPrompt.render(
        title_ja="タイトル", summary_ja=marker * 10_000
    )
    assert rendered.count(marker) == DeepSeekClassificationPrompt.MAX_SUMMARY_CHARS


def test_gemini_render_does_not_truncate_summary() -> None:
    """Gemini には truncation がない (Stage 1 出力は短い前提)。"""
    marker = "@"
    assert marker not in GeminiClassificationPrompt.TEMPLATE
    rendered = GeminiClassificationPrompt.render(
        title_ja="タイトル", summary_ja=marker * 10_000
    )
    assert rendered.count(marker) == 10_000


def test_versions_differ_between_providers() -> None:
    """model + gen_config + schema が違うので hash も別物。"""
    assert GeminiClassificationPrompt.VERSION != DeepSeekClassificationPrompt.VERSION


def test_template_is_shared_classification_prompt() -> None:
    """両 Prompt class の ``TEMPLATE`` は ``CLASSIFICATION_PROMPT`` を share する。"""
    assert GeminiClassificationPrompt.TEMPLATE is CLASSIFICATION_PROMPT
    assert DeepSeekClassificationPrompt.TEMPLATE is CLASSIFICATION_PROMPT


def test_gemini_response_schema_is_pydantic_class() -> None:
    """Gemini は Pydantic class を ``response_schema`` に渡す。"""
    from app.analysis.classifier.schema import ClassificationRawResponse

    assert GeminiClassificationPrompt.RESPONSE_SCHEMA is ClassificationRawResponse


def test_deepseek_response_schema_is_dict_tool_schema() -> None:
    """DeepSeek は dict (tool schema) を渡す (``$ref``/``$defs`` を inline 化済み)。"""
    from app.analysis.classifier.schema_tool import CLASSIFICATION_TOOL_SCHEMA

    # MappingProxyType に包んでいるので equality (==) で比較
    assert (
        dict(DeepSeekClassificationPrompt.RESPONSE_SCHEMA) == CLASSIFICATION_TOOL_SCHEMA
    )


def test_classifier_classes_use_prompt_model() -> None:
    """``MODEL`` は Prompt class を一元参照する。"""
    from app.analysis.classifier.deepseek import DeepSeekClassifier
    from app.analysis.classifier.gemini import GeminiClassifier

    assert GeminiClassifier.MODEL == GeminiClassificationPrompt.MODEL
    assert DeepSeekClassifier.MODEL == DeepSeekClassificationPrompt.MODEL


# ---------------------------------------------------------------------------
# to_domain regression — PR2 で ``InScope.category`` の型を ``ValidCategory`` →
# ``InScopeCategory`` に変更したことに伴い、``to_domain`` の明示変換が 13 値で
# 動くことを固定する。``to_domain`` 自体は PR3 で削除予定だが、PR2 merge 後
# PR3 着手前までの間 production 経路で生きているため regression 必須。
# ---------------------------------------------------------------------------


class TestToDomainCategoryConversion:
    """to_domain の ValidCategory → InScopeCategory 明示変換が全 12 値で動く。"""

    @pytest.mark.parametrize(
        "valid_slug,expected_in_scope_slug",
        [
            ("ai", "ai"),
            ("bio", "bio"),
            ("computing", "computing"),
            ("energy", "energy"),
            ("materials", "materials"),
            ("mobility", "mobility"),
            ("network", "network"),
            ("other", "other"),
            ("robotics", "robotics"),
            ("security", "security"),
            ("semiconductor", "semiconductor"),
            ("space", "space"),
        ],
    )
    def test_in_scope_category_converts_correctly(
        self,
        valid_slug: str,
        expected_in_scope_slug: str,
    ) -> None:
        from app.analysis.classifier.prompts import to_domain
        from app.analysis.classifier.schema import (
            ClassificationRawResponse,
            InScope,
            InScopeCategory,
            ValidCategory,
        )
        from app.analysis.domain.value_objects.topic import TopicName

        raw = ClassificationRawResponse(
            category=ValidCategory(valid_slug),
            topic=TopicName(root="ai"),
            investor_take="x",
        )
        result = to_domain(raw)
        assert isinstance(result, InScope)
        # 型強制: InScopeCategory のインスタンスに変換されている
        assert isinstance(result.category, InScopeCategory)
        assert result.category.value == expected_in_scope_slug


class TestToDomainOutOfScopeBranch:
    """to_domain の OUT_OF_SCOPE 振り分けが PR2 後も維持されている。"""

    def test_out_of_scope_returns_out_of_scope_instance(self) -> None:
        from app.analysis.classifier.prompts import to_domain
        from app.analysis.classifier.schema import (
            ClassificationRawResponse,
            OutOfScope,
            ValidCategory,
        )
        from app.analysis.domain.value_objects.topic import TopicName

        raw = ClassificationRawResponse(
            category=ValidCategory.OUT_OF_SCOPE,
            topic=TopicName(root="ignored"),
            investor_take="not relevant",
        )
        result = to_domain(raw)
        assert isinstance(result, OutOfScope)
        assert result.investor_take == "not relevant"
