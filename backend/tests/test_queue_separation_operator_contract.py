"""Queue 分割に伴う operator / topology artifact の静的契約テスト。"""

from __future__ import annotations

import ast
import re
import shlex
import tomllib
import unicodedata
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPOSITORY_ROOT / "Makefile"
_REDIS_FLY_CONFIG = _REPOSITORY_ROOT / "infra" / "redis" / "fly.toml"
_ARCHITECTURE_DOC = _REPOSITORY_ROOT / "docs" / "architecture.md"
_REDIS_TOPOLOGY_SPEC = (
    _REPOSITORY_ROOT / "backend" / "specs" / "redis-production-topology.md"
)
_COMPOSE_FILE = _REPOSITORY_ROOT / "docker-compose.yml"
_FLY_COLLECT_CONFIG = _REPOSITORY_ROOT / "backend" / "fly.collect.toml"
_FETCH_SUPERVISOR_CONFIG = _REPOSITORY_ROOT / "backend" / "supervisord" / "fetch.conf"
_BROKERS_MODULE = _REPOSITORY_ROOT / "backend" / "app" / "queue" / "brokers.py"


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


def _comment_text(path: Path) -> str:
    return "\n".join(
        line.lstrip()[1:].strip()
        for line in _required_text(path).splitlines()
        if line.lstrip().startswith("#")
    )


def _module_docstring(path: Path) -> str:
    docstring = ast.get_docstring(ast.parse(_required_text(path)))
    assert docstring is not None, f"module docstring is missing: {path}"
    return docstring


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


def test_pipeline_status_names_the_full_four_stage_pipeline() -> None:
    target = _normalized(_make_target("pipeline-status"))

    assert (
        "analysis stream観測" not in target
        and "curation / assessment stream status" not in target
        and "pipeline" in target
        and "stream" in target
        and _contains_any(
            target,
            ("4-stage", "4 stage", "4段", "四段", "4ステージ", "全4"),
        )
    )


def test_collect_redis_acl_has_only_required_queue_and_taskiq_key_surfaces() -> None:
    key_patterns = {
        token for token in _redis_acl_tokens("collect") if token.startswith("~")
    }

    assert key_patterns == {
        "~pipeline:metadata",
        "~pipeline:acquisition",
        "~pipeline:completion",
        "~pipeline:curation",
        "~autoclaim:taskiq:pipeline:metadata",
        "~autoclaim:taskiq:pipeline:acquisition",
        "~autoclaim:taskiq:pipeline:completion",
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


def test_architecture_describes_collection_control_and_multistream_worker() -> None:
    section = _normalized(
        _markdown_section(_required_text(_ARCHITECTURE_DOC), "非同期パイプライン")
    )

    assert (
        all(
            term in section
            for term in (
                "pipeline:metadata",
                "pipeline:acquisition",
                "pipeline:completion",
                "broker_content",
                "taskiq",
                "dispatch",
                "acquisition",
                "completion",
                "concurrency 5",
            )
        )
        and _contains_any(section, ("control", "制御", "sweep"))
        and _contains_any(section, ("共有 worker", "共有worker", "shared worker"))
        and _contains_any(section, ("1 process", "1プロセス", "1つの process"))
    )


def test_architecture_separates_collection_visibility_but_shares_capacity() -> None:
    section = _normalized(
        _markdown_section(_required_text(_ARCHITECTURE_DOC), "非同期パイプライン")
    )

    assert (
        all(
            term in section
            for term in (
                "pipeline:acquisition",
                "pipeline:completion",
                "stream",
                "group",
                "age",
                "db pool",
                "backpressure",
            )
        )
        and _contains_any(section, ("retention", "retained", "保持", "maxlen"))
        and _contains_any(section, ("worker slot", "worker の slot"))
        and _contains_any(
            section,
            ("failure domain", "failure isolation", "障害境界", "障害分離"),
        )
        and _contains_any(section, ("共有する", "共有した", "共有の", "shared"))
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


def test_redis_topology_spec_records_all_four_stage_stream_rows() -> None:
    spec = _required_text(_REDIS_TOPOLOGY_SPEC)
    expected_consumers = {
        "pipeline:acquisition": "broker_content",
        "pipeline:completion": "broker_content",
        "pipeline:curation": "broker_analysis",
        "pipeline:assessment": "broker_analysis",
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


def test_redis_topology_spec_records_metadata_control_and_greenfield_legacy() -> None:
    spec = _required_text(_REDIS_TOPOLOGY_SPEC)
    metadata = _paragraph_containing(spec, "pipeline:metadata")
    legacy_content = _paragraph_containing(spec, "pipeline:content")

    assert (
        "dispatch" in metadata
        and _contains_any(metadata, ("control", "制御", "sweep"))
        and _contains_any(legacy_content, ("legacy", "旧stream", "旧 stream"))
        and _contains_any(
            legacy_content,
            ("作らない", "作成しない", "存在しない", "引き継がない"),
        )
        and _contains_any(
            legacy_content,
            ("migrationを行わない", "migration は行わない", "移行しない"),
        )
    )


def test_redis_topology_spec_records_final_collect_acl_boundary() -> None:
    section = _normalized(
        _markdown_section(_required_text(_REDIS_TOPOLOGY_SPEC), "ACL boundary")
    )
    allowed = (
        "~pipeline:metadata",
        "~pipeline:acquisition",
        "~pipeline:completion",
        "~pipeline:curation",
        "~autoclaim:taskiq:pipeline:metadata",
        "~autoclaim:taskiq:pipeline:acquisition",
        "~autoclaim:taskiq:pipeline:completion",
        "~taskiq:*",
    )

    assert (
        all(pattern in section for pattern in allowed)
        and all(
            stream in section
            for stream in (
                "pipeline:content",
                "pipeline:assessment",
                "pipeline:embedding",
                "pipeline:maintenance",
            )
        )
        and _contains_any(section, ("拒否", "許可しない", "公開しない", "削除"))
        and all(term in section for term in ("core", "~*", "&*", "+@all"))
    )


def test_topology_spec_records_four_stage_freshness_and_completion_alerts() -> None:
    section = _normalized(
        _markdown_section(
            _required_text(_REDIS_TOPOLOGY_SPEC),
            "Monitoring / operator contract",
        )
    )

    assert (
        all(
            stage in section
            for stage in ("acquisition", "completion", "curation", "assessment")
        )
        and _contains_any(section, ("4-stage", "4 stage", "4段", "4ステージ"))
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


def test_redis_topology_spec_records_collection_replay_safety_runbook() -> None:
    spec = _normalized(_required_text(_REDIS_TOPOLOGY_SPEC))

    stop_boundary = (
        all(
            term in spec
            for term in ("scheduler", "worker-fetch", "metadata", "content")
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
                "rename slice",
                "release",
            )
        )
        and _contains_any(spec, ("公開を止め", "公開停止", "stop release"))
        and _contains_any(spec, ("rename slice 後", "rename slice完了後"))
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


def test_fetch_deployment_comments_match_control_and_multistream_roles() -> None:
    fly_comments = _normalized(_comment_text(_FLY_COLLECT_CONFIG))
    supervisor_comments = _normalized(_comment_text(_FETCH_SUPERVISOR_CONFIG))

    role_terms = (
        "metadata",
        "dispatch",
        "pipeline:acquisition",
        "pipeline:completion",
        "broker_content",
    )
    shared_consumer_terms = (
        "2 stream",
        "2つの stream",
        "両 stream",
        "multi-stream",
    )

    assert (
        all(term in fly_comments for term in role_terms)
        and _contains_any(fly_comments, ("control", "制御", "sweep"))
        and _contains_any(fly_comments, shared_consumer_terms)
        and _contains_any(
            fly_comments, ("共有 consumer", "共有consumer", "shared consumer")
        )
        and "broker_metadata=acquisition+dispatch" not in fly_comments
        and "broker_content=completion" not in fly_comments
    )
    assert (
        all(term in supervisor_comments for term in role_terms)
        and _contains_any(supervisor_comments, ("control", "制御", "sweep"))
        and _contains_any(supervisor_comments, shared_consumer_terms)
        and _contains_any(
            supervisor_comments,
            ("共有 consumer", "共有consumer", "shared consumer"),
        )
    )


def test_broker_module_docstring_matches_control_and_multistream_roles() -> None:
    docstring = _normalized(_module_docstring(_BROKERS_MODULE))

    assert (
        all(
            term in docstring
            for term in (
                "broker_metadata",
                "dispatch",
                "broker_content",
                "acquisition",
                "completion",
            )
        )
        and _contains_any(docstring, ("control", "制御", "sweep"))
        and _contains_any(
            docstring,
            ("2 stream", "2つの stream", "両 stream", "multi-stream"),
        )
        and _contains_any(
            docstring, ("共有 consumer", "共有consumer", "shared consumer")
        )
        and "rss/hn メタデータ取得 + dispatch" not in docstring
        and "記事単位のコンテンツ抽出" not in docstring
    )
