"""External search DeepSeek spec and schema tests."""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from app.agent.external_search import (
    EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK,
    EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK,
    EXTERNAL_TASK_QUERY_LIMIT,
)
from app.agent.external_search.ai.schema_tool import (
    EVIDENCE_SELECTOR_TOOL_SCHEMA,
    QUERY_GENERATOR_TOOL_SCHEMA,
)
from app.agent.external_search.ai.spec import (
    DEEPSEEK_EVIDENCE_SELECTOR_SPEC,
    DEEPSEEK_QUERY_GENERATOR_SPEC,
    EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
)
from app.agent.external_search.runner import (
    EVIDENCE_SELECT_TIMEOUT_SECONDS,
    QUERY_GENERATE_TIMEOUT_SECONDS,
)
from app.analysis.prompt_versions import compute_call_signature
from app.analysis.rate_limit import AIModelRateLimitPolicy

_HEX8 = re.compile(r"^[0-9a-f]{8}$")
_UNSUPPORTED_DEEPSEEK_STRICT_KEYWORDS = {
    "maxLength",
    "minLength",
    "minItems",
    "maxItems",
}


def _collect_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_collect_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_collect_keys(child))
    return keys


@pytest.mark.parametrize(
    "schema",
    [QUERY_GENERATOR_TOOL_SCHEMA, EVIDENCE_SELECTOR_TOOL_SCHEMA],
)
def test_tool_schemas_do_not_use_deepseek_strict_unsupported_keywords(
    schema: dict[str, Any],
) -> None:
    assert _collect_keys(schema).isdisjoint(_UNSUPPORTED_DEEPSEEK_STRICT_KEYWORDS)


def test_query_generator_description_uses_query_limit_constant() -> None:
    description = QUERY_GENERATOR_TOOL_SCHEMA["properties"]["queries"]["description"]

    assert str(EXTERNAL_TASK_QUERY_LIMIT) in description


def test_evidence_selector_descriptions_use_external_search_cap_constants() -> None:
    properties = EVIDENCE_SELECTOR_TOOL_SCHEMA["properties"]

    assert (
        str(EXTERNAL_SEARCH_EVIDENCE_LIMIT_PER_TASK)
        in properties["selections"]["description"]
    )
    assert (
        str(EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK)
        in properties["missing"]["description"]
    )


@pytest.mark.parametrize(
    "schema",
    [QUERY_GENERATOR_TOOL_SCHEMA, EVIDENCE_SELECTOR_TOOL_SCHEMA],
)
def test_tool_schemas_are_inline_strict_objects(schema: dict[str, Any]) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "$ref" not in _collect_keys(schema)
    assert "$defs" not in _collect_keys(schema)


def test_query_generator_schema_requires_queries_only() -> None:
    assert QUERY_GENERATOR_TOOL_SCHEMA["required"] == ["queries"]


def test_evidence_selector_schema_requires_selections_and_missing() -> None:
    assert EVIDENCE_SELECTOR_TOOL_SCHEMA["required"] == ["selections", "missing"]


def test_specs_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        DEEPSEEK_QUERY_GENERATOR_SPEC.tool_name = "other"  # type: ignore[misc]


def test_specs_expose_deepseek_beta_endpoint() -> None:
    assert DEEPSEEK_QUERY_GENERATOR_SPEC.base_url == "https://api.deepseek.com/beta"
    assert DEEPSEEK_EVIDENCE_SELECTOR_SPEC.base_url == "https://api.deepseek.com/beta"


def test_specs_use_deepseek_v4_flash() -> None:
    assert DEEPSEEK_QUERY_GENERATOR_SPEC.model == "deepseek-v4-flash"
    assert DEEPSEEK_EVIDENCE_SELECTOR_SPEC.model == "deepseek-v4-flash"


def test_specs_have_distinct_tool_names() -> None:
    assert DEEPSEEK_QUERY_GENERATOR_SPEC.tool_name == "generate_search_queries"
    assert DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name == "select_evidence"


def test_structured_output_forces_matching_tool_choice() -> None:
    for spec in [DEEPSEEK_QUERY_GENERATOR_SPEC, DEEPSEEK_EVIDENCE_SELECTOR_SPEC]:
        assert spec.structured_output["tool_choice"]["function"]["name"] == (
            spec.tool_name
        )
        assert spec.structured_output["extra_body"]["thinking"]["type"] == "disabled"


def test_gen_config_uses_step_specific_token_budgets() -> None:
    assert DEEPSEEK_QUERY_GENERATOR_SPEC.gen_config == {"max_tokens": 256}
    assert DEEPSEEK_EVIDENCE_SELECTOR_SPEC.gen_config == {"max_tokens": 2048}


def test_rate_limit_policy_has_no_rules() -> None:
    for spec in [DEEPSEEK_QUERY_GENERATOR_SPEC, DEEPSEEK_EVIDENCE_SELECTOR_SPEC]:
        assert spec.rate_limit_policy == AIModelRateLimitPolicy(
            provider="deepseek",
            model="deepseek-v4-flash",
            rules=(),
        )


def test_timeout_is_inside_runner_backstops() -> None:
    assert EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS < QUERY_GENERATE_TIMEOUT_SECONDS
    assert EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS < EVIDENCE_SELECT_TIMEOUT_SECONDS


def test_specs_carry_timeout_constant_for_client_construction() -> None:
    assert DEEPSEEK_QUERY_GENERATOR_SPEC.timeout_seconds == (
        EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS
    )
    assert DEEPSEEK_EVIDENCE_SELECTOR_SPEC.timeout_seconds == (
        EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS
    )


def test_versions_are_hex8() -> None:
    assert _HEX8.fullmatch(DEEPSEEK_QUERY_GENERATOR_SPEC.version) is not None
    assert _HEX8.fullmatch(DEEPSEEK_EVIDENCE_SELECTOR_SPEC.version) is not None


def test_query_generator_version_comes_from_call_signature() -> None:
    expected = compute_call_signature(
        prompt_template=DEEPSEEK_QUERY_GENERATOR_SPEC.prompt_template,
        model=DEEPSEEK_QUERY_GENERATOR_SPEC.model,
        gen_config={
            **DEEPSEEK_QUERY_GENERATOR_SPEC.gen_config,
            **DEEPSEEK_QUERY_GENERATOR_SPEC.structured_output,
        },
        response_schema=DEEPSEEK_QUERY_GENERATOR_SPEC.response_schema,
        system_instruction=DEEPSEEK_QUERY_GENERATOR_SPEC.system_instruction,
    )

    assert DEEPSEEK_QUERY_GENERATOR_SPEC.version == expected


def test_evidence_selector_version_comes_from_call_signature() -> None:
    expected = compute_call_signature(
        prompt_template=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.prompt_template,
        model=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.model,
        gen_config={
            **DEEPSEEK_EVIDENCE_SELECTOR_SPEC.gen_config,
            **DEEPSEEK_EVIDENCE_SELECTOR_SPEC.structured_output,
        },
        response_schema=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.response_schema,
        system_instruction=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.system_instruction,
    )

    assert DEEPSEEK_EVIDENCE_SELECTOR_SPEC.version == expected
