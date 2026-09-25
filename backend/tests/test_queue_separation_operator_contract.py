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
_REDIS_TOPOLOGY_SPEC = (
    _REPOSITORY_ROOT / "backend" / "specs" / "redis-production-topology.md"
)
_COMPOSE_FILE = _REPOSITORY_ROOT / "docker-compose.yml"


def _required_text(path: Path) -> str:
    assert path.is_file(), f"required operator artifact is missing: {path}"
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


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


def _markdown_row_containing(text: str, token: str) -> str:
    row = next(
        (
            line
            for line in text.splitlines()
            if line.lstrip().startswith("|") and token in line
        ),
        None,
    )
    assert row is not None, f"markdown topology row is missing: {token}"
    return _normalized(row)


def _paragraph_containing(text: str, token: str) -> str:
    paragraph = next(
        (paragraph for paragraph in re.split(r"\n\s*\n", text) if token in paragraph),
        None,
    )
    assert paragraph is not None, f"markdown paragraph is missing: {token}"
    return _normalized(paragraph)


def _compose_comment_text() -> str:
    return "\n".join(
        line.lstrip()[1:].strip()
        for line in _required_text(_COMPOSE_FILE).splitlines()
        if line.lstrip().startswith("#")
    )


def test_makefile_does_not_keep_dead_queues_variable() -> None:
    makefile = _required_text(_MAKEFILE)

    assert re.search(r"(?m)^\s*QUEUES\s*(?::=|\?=|\+=|=)", makefile) is None


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


def test_collect_redis_acl_has_only_required_queue_and_autoclaim_key_surfaces() -> None:
    key_patterns = {
        token for token in _redis_acl_tokens("collect") if token.startswith("~")
    }

    assert key_patterns == {
        "~pipeline:dispatch",
        "~pipeline:acquisition",
        "~pipeline:completion",
        "~pipeline:curation",
        "~autoclaim:taskiq:pipeline:dispatch",
        "~autoclaim:taskiq:pipeline:acquisition",
        "~autoclaim:taskiq:pipeline:completion",
    }


def test_collect_redis_acl_has_only_required_command_surface() -> None:
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
        "+multi",
        "+exec",
        "-@dangerous",
    }


def test_core_redis_acl_remains_broad() -> None:
    tokens = _redis_acl_tokens("core")

    assert {"~*", "&*", "+@all"}.issubset(tokens)


def test_redis_topology_spec_records_all_three_stage_stream_rows() -> None:
    spec = _required_text(_REDIS_TOPOLOGY_SPEC)
    expected_consumers = {
        "pipeline:acquisition": "broker_collection",
        "pipeline:completion": "broker_collection",
        "pipeline:curation": "broker_analysis",
    }

    rows = {
        stream: _markdown_row_containing(spec, stream) for stream in expected_consumers
    }

    assert {
        stream: (
            "taskiq" in row,
            "10,000" in row,
            expected_consumers[stream] in row,
        )
        for stream, row in rows.items()
    } == {stream: (True, True, True) for stream in expected_consumers}


def test_redis_topology_spec_records_dispatch_control_and_greenfield_legacy() -> None:
    spec = _required_text(_REDIS_TOPOLOGY_SPEC)
    dispatch = _paragraph_containing(spec, "pipeline:dispatch")
    legacy_content = _paragraph_containing(spec, "pipeline:content")

    assert (
        _contains_any(dispatch, ("control", "制御", "sweep"))
        and _contains_any(legacy_content, ("legacy", "旧stream", "旧 stream"))
        and _contains_any(
            legacy_content,
            ("作らない", "作成しない", "存在しない", "引き継がない"),
        )
        and _contains_any(
            legacy_content,
            ("migrationを行わない", "migration は行わない", "移行しない"),
        )
        and "pipeline:metadata" not in spec
    )


def test_redis_topology_spec_records_final_collect_acl_boundary() -> None:
    section = _normalized(
        _markdown_section(_required_text(_REDIS_TOPOLOGY_SPEC), "ACL boundary")
    )
    allowed = (
        "~pipeline:dispatch",
        "~pipeline:acquisition",
        "~pipeline:completion",
        "~pipeline:curation",
        "~autoclaim:taskiq:pipeline:dispatch",
        "~autoclaim:taskiq:pipeline:acquisition",
        "~autoclaim:taskiq:pipeline:completion",
    )

    assert (
        all(pattern in section for pattern in allowed)
        and all(
            stream in section
            for stream in (
                "pipeline:content",
                "pipeline:maintenance",
            )
        )
        and _contains_any(section, ("拒否", "許可しない", "公開しない", "削除"))
        and all(term in section for term in ("core", "~*", "&*", "+@all"))
    )


def test_topology_spec_records_three_stage_freshness_and_completion_alerts() -> None:
    section = _normalized(
        _markdown_section(
            _required_text(_REDIS_TOPOLOGY_SPEC),
            "Monitoring / operator contract",
        )
    )

    assert (
        all(stage in section for stage in ("acquisition", "completion", "curation"))
        and _contains_any(section, ("3-stage", "3 stage", "3段", "3ステージ"))
        and _contains_any(section, ("3分", "3 分", "3 minutes"))
        and all(
            term in section
            for term in (
                "observation_up",
                "observation_timestamp",
                "120",
                "300",
                "warning",
                "critical",
                "vector.completion.lease_swept",
            )
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


def test_redis_topology_spec_explains_approximate_maxlen_and_ghost_pel() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    assert (
        "maxlen" in spec
        and _contains_any(spec, ("approximate", "近似", "~10,000"))
        and "ghost pel" in spec
        and _contains_any(spec, ("上限ではない", "hard upper bound", "拘束されない"))
    )


def test_redis_topology_spec_records_collection_replay_safety_runbook() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    stop_boundary = (
        all(
            term in spec
            for term in ("scheduler", "worker-fetch", "dispatch", "collection")
        )
        and _contains_any(spec, ("停止", "stop"))
        and all(term in spec for term in ("admin", "fetch", "禁止"))
    )
    state_and_cost_gate = (
        all(
            term in spec
            for term in (
                "retained",
                "pel",
                "db",
                "10,000",
                "acquisition",
                "http",
                "ai",
            )
        )
        and _contains_any(spec, ("live feed", "live 再取得", "live feed 再取得"))
        and _contains_any(spec, ("重複", "duplicate"))
        and _contains_any(spec, ("承認", "受容", "approve"))
    )
    restart_order_and_non_destructive_recovery = (
        _contains_any(
            spec,
            ("worker-fetch を再起動", "worker-fetchを再起動", "restart worker-fetch"),
        )
        and _contains_any(spec, ("scheduler を再開", "schedulerを再開"))
        and _contains_any(spec, ("最後に admin", "admin fetch を最後", "adminを最後"))
        and all(term in spec for term in ("del", "xtrim"))
        and _contains_any(spec, ("使わない", "使用しない", "禁止"))
    )

    assert (
        stop_boundary,
        state_and_cost_gate,
        restart_order_and_non_destructive_recovery,
    ) == (True, True, True)


def test_redis_topology_spec_records_collection_capacity_and_release_gate() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    assert (
        "collection" in spec
        and _contains_any(
            spec,
            (
                "1 → 2 stream",
                "1→2 stream",
                "1本→2本",
                "1 本 → 2 本",
                "1本から2本",
                "1 本から 2 本",
            ),
        )
        and all(term in spec for term in ("4.9", "planning estimate", "noeviction"))
        and _contains_any(spec, ("approximate", "近似"))
        and "ghost pel" in spec
        and all(
            term in spec
            for term in (
                "80%",
                "used_memory",
                "used_memory_peak",
                "memory usage",
                "worker rss",
                "pipeline:dispatch",
                "release",
            )
        )
        and _contains_any(spec, ("公開を止め", "公開停止", "stop release"))
        and _contains_any(spec, ("最終topology", "final topology"))
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
