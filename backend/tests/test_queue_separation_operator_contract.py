"""Queue 分割に伴う operator / topology artifact の静的契約テスト。"""

from __future__ import annotations

import re
import shlex
import tomllib
import unicodedata
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPOSITORY_ROOT / "Makefile"
_REDIS_FLY_CONFIG = _REPOSITORY_ROOT / "infra" / "redis" / "fly.toml"
_ARCHITECTURE_DOC = _REPOSITORY_ROOT / "docs" / "architecture.md"
_REDIS_TOPOLOGY_SPEC = _REPOSITORY_ROOT / "specs" / "redis-production-topology.md"
_COMPOSE_FILE = _REPOSITORY_ROOT / "docker-compose.yml"


def _required_text(path: Path) -> str:
    assert path.is_file(), f"required operator artifact is missing: {path}"
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _makefile_words(variable: str) -> set[str]:
    lines = _required_text(_MAKEFILE).splitlines()
    for index, line in enumerate(lines):
        match = re.match(rf"^{re.escape(variable)}\s*:?=\s*(?P<value>.*)$", line)
        if match is None:
            continue

        value_parts = [match.group("value")]
        while value_parts[-1].rstrip().endswith("\\"):
            value_parts[-1] = value_parts[-1].rstrip()[:-1]
            index += 1
            assert index < len(lines), f"Makefile variable {variable} is incomplete"
            value_parts.append(lines[index].strip())
        return set(shlex.split(" ".join(value_parts)))

    raise AssertionError(f"Makefile variable {variable} is missing")


def _make_target(target: str) -> str:
    lines = _required_text(_MAKEFILE).splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^{re.escape(target)}\s*:", line)
        ),
        None,
    )
    assert start is not None, f"Makefile target {target} is missing"

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if (
            line
            and not line.startswith((" ", "\t", "#"))
            and re.match(r"^[A-Za-z0-9_.%/-]+\s*:", line)
        ):
            end = index
            break
    return "\n".join(lines[start:end])


def _redis_acl_tokens(user: str) -> set[str]:
    config = tomllib.loads(_required_text(_REDIS_FLY_CONFIG))
    redis_command = config["processes"]["redis"]
    match = re.search(rf'echo "user {re.escape(user)} (?P<rules>[^"]+)"', redis_command)
    assert match is not None, f"Redis ACL for {user} is missing"
    return set(shlex.split(match.group("rules")))


def _markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    heading_pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$")
    start = next(
        (index for index, line in enumerate(lines) if heading_pattern.match(line)),
        None,
    )
    assert start is not None, f"markdown section is missing: {heading}"

    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^##\s+", lines[index])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _contains_any(text: str, choices: tuple[str, ...]) -> bool:
    return any(choice in text for choice in choices)


def _compose_comment_text() -> str:
    return "\n".join(
        line.lstrip()[1:].strip()
        for line in _required_text(_COMPOSE_FILE).splitlines()
        if line.lstrip().startswith("#")
    )


def _worker_analysis_comment_context() -> str:
    lines = _required_text(_COMPOSE_FILE).splitlines()
    service_line = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"  worker-analysis:", line)
        ),
        None,
    )
    assert service_line is not None, "worker-analysis service is missing"
    return "\n".join(lines[max(0, service_line - 12) : service_line])


def test_makefile_observes_split_analysis_streams_without_legacy_stream() -> None:
    queues = _makefile_words("QUEUES")

    assert {
        "pipeline:curation",
        "pipeline:assessment",
    }.issubset(queues) and "pipeline:analysis" not in queues


def test_pipeline_status_delegates_queue_semantics_to_backend_adapter() -> None:
    target = _normalized(_make_target("pipeline-status"))

    assert (
        re.search(
            r"docker\s+compose\s+exec(?:\s+\S+)*\s+backend\s+"
            r"(?:uv\s+run\s+)?python\s+\S*scripts/pipeline_queue_status\.py",
            target,
        )
        is not None
    )


def test_pipeline_status_does_not_reimplement_raw_redis_stream_semantics() -> None:
    target = _normalized(_make_target("pipeline-status"))

    assert (
        re.search(r"\bredis-cli\b[^\n]*(?:xlen|xpending|xinfo|xrange)", target) is None
    )


def test_pipeline_status_does_not_call_retained_entries_queue_depth() -> None:
    target = _normalized(_make_target("pipeline-status"))

    assert not _contains_any(target, ("queue depth", "backlog", "キュー深度"))


def test_collect_redis_acl_has_only_required_queue_and_taskiq_key_surfaces() -> None:
    key_patterns = {
        token for token in _redis_acl_tokens("collect") if token.startswith("~")
    }

    assert key_patterns == {
        "~pipeline:metadata",
        "~pipeline:content",
        "~pipeline:curation",
        "~autoclaim:taskiq:pipeline:metadata",
        "~autoclaim:taskiq:pipeline:content",
        "~taskiq:*",
    }


def test_collect_redis_acl_preserves_existing_command_surface() -> None:
    tokens = _redis_acl_tokens("collect")
    command_rules = {
        token
        for token in tokens
        if token == "resetchannels" or token.startswith(("+", "-"))
    }

    assert command_rules == {
        "resetchannels",
        "+@connection",
        "+@read",
        "+@write",
        "+@stream",
        "+@scripting",
        "-@dangerous",
    }


def test_core_redis_acl_remains_broad() -> None:
    tokens = _redis_acl_tokens("core")

    assert {"~*", "&*", "+@all"}.issubset(tokens)


def test_architecture_describes_two_logical_streams_on_one_shared_worker() -> None:
    section = _normalized(
        _markdown_section(_required_text(_ARCHITECTURE_DOC), "非同期パイプライン")
    )

    assert (
        "pipeline:curation" in section
        and "pipeline:assessment" in section
        and "broker_analysis" in section
        and "taskiq" in section
        and "10" in section
        and _contains_any(section, ("共有 worker", "共有worker", "shared worker"))
        and "process" in section
        and _contains_any(section, ("1つ", "1 process", "1プロセス", "one process"))
    )


def test_architecture_states_visibility_and_shared_resource_boundaries() -> None:
    section = _normalized(
        _markdown_section(_required_text(_ARCHITECTURE_DOC), "非同期パイプライン")
    )

    assert (
        _contains_any(section, ("retained", "保持量", "保持件数"))
        and all(term in section for term in ("lag", "pending"))
        and _contains_any(section, ("age", "経過時間"))
        and all(term in section for term in ("concurrency", "backpressure"))
        and _contains_any(
            section,
            ("failure isolation", "failure domain", "障害分離", "障害境界"),
        )
        and _contains_any(section, ("独立しない", "分離しない", "未分離", "共有する"))
    )


def test_redis_topology_spec_names_greenfield_two_stream_live_topology() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    assert (
        "pipeline:curation" in spec
        and "pipeline:assessment" in spec
        and "pipeline:analysis" in spec
        and _contains_any(spec, ("greenfield", "初回公開前", "未デプロイ"))
        and _contains_any(spec, ("legacy", "旧stream", "旧 stream"))
        and _contains_any(spec, ("存在しない", "作らない", "削除", "除外"))
        and _contains_any(
            spec,
            (
                "migration不要",
                "migrationは不要",
                "migration は不要",
                "migrationを行わない",
                "migrationは行わない",
                "移行不要",
                "移行を行わない",
            ),
        )
    )


def test_redis_topology_spec_distinguishes_retention_and_live_group_state() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    assert (
        all(
            term in spec
            for term in (
                "retained entries",
                "xlen",
                "lag",
                "pending",
                "enqueue age",
            )
        )
        and _contains_any(spec, ("ack 済み", "ack済み"))
        and _contains_any(spec, ("区別", "異なる", "別の指標"))
    )


def test_redis_topology_spec_records_final_two_stream_memory_tradeoff() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    assert (
        _contains_any(spec, ("2 stream", "2stream", "2本", "2つのstream"))
        and "20,000" in spec
        and "9.84 mb" in spec
        and "4.92 mb" in spec
        and _contains_any(spec, ("trade-off", "tradeoff", "トレードオフ"))
    )


def test_redis_topology_spec_explains_approximate_maxlen_and_ghost_pel() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    assert (
        "maxlen" in spec
        and _contains_any(spec, ("approximate", "近似", "~10,000"))
        and "ghost pel" in spec
        and _contains_any(spec, ("上限ではない", "hard upper bound", "拘束されない"))
    )


def test_compose_comment_calls_maxlen_retained_history_not_backlog() -> None:
    comments = _normalized(_compose_comment_text())

    assert (
        "maxlen" in comments
        and _contains_any(comments, ("retained", "保持履歴", "保持 entry", "保持件数"))
        and _contains_any(
            comments,
            (
                "backlog ではなく",
                "backlogではなく",
                "not backlog",
                "queue depth ではなく",
                "キュー深度ではなく",
            ),
        )
    )


def test_compose_comment_describes_analysis_broker_as_shared_stream_consumer() -> None:
    context = _normalized(_worker_analysis_comment_context())

    assert (
        "broker_analysis" in context
        and "curation" in context
        and "assessment" in context
        and _contains_any(context, ("共有 worker", "共有worker", "shared worker"))
        and _contains_any(context, ("consume", "consumer", "購読", "読み取"))
    )
