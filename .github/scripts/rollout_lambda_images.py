"""backendイメージで動くLambdaをrelease SHAのイメージへ更新し、収束を検証する。"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SUCCESS = 0
ROLLOUT_FAILURE = 1
CONTRACT_FAILURE = 2

_NAME_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_REPOSITORY_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{1,255}$")
_IMAGE_TAG_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_ARN_RE = re.compile(r"arn:[a-z0-9-]+:[^\s|,;)]+")


class RolloutInputError(ValueError):
    """更新を始められない入力契約違反。"""


class AwsCliError(RuntimeError):
    """AWS CLI呼び出し自体の失敗。"""


class LambdaImageClient(Protocol):
    """Lambdaのイメージ更新が使うAWS境界。"""

    def resolve_image_digest(self, repository: str, image_tag: str) -> str | None: ...

    def list_functions(self) -> Sequence[Mapping[str, object]]: ...

    def get_function(self, name: str) -> Mapping[str, object]: ...

    def update_function_image(self, name: str, image_uri: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RolloutConfig:
    """1回のLambdaイメージ更新条件。"""

    name_prefix: str
    repository: str
    image_tag: str
    timeout_seconds: float
    poll_seconds: float
    summary_file: Path


@dataclass(slots=True)
class FunctionObservation:
    """1関数の更新前後の観測結果。"""

    name: str
    repository_uri: str
    action: str
    state: str = ""
    update_status: str = ""
    reason: str = ""
    digest: str = ""


class AwsCliLambdaImageClient:
    """AWS CLIのJSON出力だけを読むclient。"""

    def __init__(self, aws_path: str) -> None:
        self._aws_path = aws_path

    def _run(self, *arguments: str) -> Mapping[str, object]:
        # shellを使わず、引数も固定commandと内部生成値だけを渡す。
        completed = subprocess.run(  # noqa: S603
            [self._aws_path, *arguments, "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "AWS CLI returned no error detail"
            raise AwsCliError(_sanitize(detail))
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AwsCliError("AWS CLI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AwsCliError("AWS CLI response must be a JSON object")
        return payload

    def resolve_image_digest(self, repository: str, image_tag: str) -> str | None:
        try:
            payload = self._run(
                "ecr",
                "describe-images",
                "--repository-name",
                repository,
                "--image-ids",
                f"imageTag={image_tag}",
            )
        except AwsCliError as exc:
            if "ImageNotFoundException" in str(exc):
                return None
            raise
        details = payload.get("imageDetails")
        if not isinstance(details, list) or len(details) != 1:
            raise AwsCliError("ECR response must contain exactly one image")
        digest = details[0].get("imageDigest") if isinstance(details[0], dict) else None
        if not isinstance(digest, str):
            raise AwsCliError("ECR response has no image digest")
        return digest

    def list_functions(self) -> Sequence[Mapping[str, object]]:
        # AWS CLIは既定で全ページを結合して返す。
        functions = self._run("lambda", "list-functions").get("Functions")
        if not isinstance(functions, list):
            raise AwsCliError("Lambda response has no function list")
        return [item for item in functions if isinstance(item, dict)]

    def get_function(self, name: str) -> Mapping[str, object]:
        return self._run("lambda", "get-function", "--function-name", name)

    def update_function_image(self, name: str, image_uri: str) -> None:
        self._run(
            "lambda",
            "update-function-code",
            "--function-name",
            name,
            "--image-uri",
            image_uri,
        )


def rollout_lambda_images(
    config: RolloutConfig,
    client: LambdaImageClient,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """対象関数を更新して収束までpollし、成功または理由付き失敗のexit codeを返す。"""

    try:
        digest = client.resolve_image_digest(config.repository, config.image_tag)
        if digest is None:
            return _fail_globally(
                config,
                "Release image is missing",
                "backend ECRに対象SHAのイメージが無い",
            )
        if not _DIGEST_RE.fullmatch(digest):
            return _fail_globally(
                config, "ECR response contract violation", "digestの形式が不正"
            )
        targets, excluded = _select_targets(config, client, digest)
        if not targets:
            return _fail_globally(
                config, "No Lambda function to roll out", "更新対象の関数が0件だった"
            )
    except AwsCliError as exc:
        return _fail_globally(config, "AWS API error", str(exc))

    for item in targets:
        if item.action != "update":
            continue
        try:
            client.update_function_image(item.name, f"{item.repository_uri}@{digest}")
        except AwsCliError as exc:
            # 途中まで更新済みの関数を結果に残すため、全体の失敗にまとめない。
            item.action, item.reason = "request_failed", str(exc)
            return _finish(
                config,
                "Lambda image update request failed",
                targets,
                excluded,
                digest,
                exit_code=CONTRACT_FAILURE,
            )

    deadline = monotonic() + config.timeout_seconds
    while True:
        try:
            for item in targets:
                _observe(item, client.get_function(item.name))
        except AwsCliError as exc:
            return _fail_globally(config, "AWS API error", str(exc))

        if any(item.update_status == "Failed" for item in targets):
            return _finish(
                config, "Lambda image update failed", targets, excluded, digest
            )
        if all(_converged(item, digest) for item in targets):
            return _finish(config, "", targets, excluded, digest)
        if monotonic() >= deadline:
            for item in targets:
                if not _converged(item, digest):
                    item.reason = item.reason or "rollout_timeout"
            return _finish(
                config, "Lambda image rollout timed out", targets, excluded, digest
            )

        waiting = ", ".join(
            item.name for item in targets if not _converged(item, digest)
        )
        print(f"Waiting for Lambda image rollout: {waiting}")
        sleep(config.poll_seconds)


def _select_targets(
    config: RolloutConfig, client: LambdaImageClient, digest: str
) -> tuple[list[FunctionObservation], list[str]]:
    """名前とイメージの置き場で対象を決め、対象外は理由付きで返す。"""

    targets: list[FunctionObservation] = []
    excluded: list[str] = []
    prefix = f"{config.name_prefix}-"
    names = sorted(
        str(item.get("FunctionName", ""))
        for item in client.list_functions()
        if str(item.get("FunctionName", "")).startswith(prefix)
        and item.get("PackageType") == "Image"
    )
    for name in names:
        code = client.get_function(name).get("Code")
        image_uri = code.get("ImageUri") if isinstance(code, dict) else None
        resolved = code.get("ResolvedImageUri") if isinstance(code, dict) else None
        if not isinstance(image_uri, str) or not isinstance(resolved, str):
            raise AwsCliError(f"Lambda response has no image for {name}")
        repository_uri = re.split(r"[@:]", image_uri, maxsplit=1)[0]
        if not repository_uri.endswith(f"/{config.repository}"):
            excluded.append(name)
            continue
        action = "unchanged" if resolved.endswith(f"@{digest}") else "update"
        targets.append(
            FunctionObservation(name=name, repository_uri=repository_uri, action=action)
        )
    return targets, excluded


def _observe(item: FunctionObservation, function: Mapping[str, object]) -> None:
    configuration = function.get("Configuration")
    code = function.get("Code")
    if not isinstance(configuration, dict) or not isinstance(code, dict):
        raise AwsCliError(f"Lambda response is incomplete for {item.name}")
    item.state = str(configuration.get("State", ""))
    item.update_status = str(configuration.get("LastUpdateStatus", ""))
    item.reason = str(
        configuration.get("LastUpdateStatusReason")
        or configuration.get("StateReason")
        or ""
    )
    item.digest = str(code.get("ResolvedImageUri", "")).rpartition("@")[2]


def _converged(item: FunctionObservation, digest: str) -> bool:
    # 長く呼ばれていない関数はInactiveで待機し、次の呼び出しで同じイメージから起動する。
    return (
        item.update_status == "Successful"
        and item.state in {"Active", "Inactive"}
        and item.digest == digest
    )


def _finish(
    config: RolloutConfig,
    failure_title: str,
    targets: Sequence[FunctionObservation],
    excluded: Sequence[str],
    digest: str,
    *,
    exit_code: int = ROLLOUT_FAILURE,
) -> int:
    lines = [
        f"### {failure_title or 'Lambda image rollout'}",
        "",
        f"Result: **{'failure' if failure_title else 'success'}**",
        "",
        f"Image: `{config.image_tag}` (`{digest}`)",
        "",
        "| Function | Action | State | Update | Converged | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| `{_markdown(item.name)}` | {item.action} | {_markdown(item.state) or '-'} "
        f"| {_markdown(item.update_status) or '-'} "
        f"| {'yes' if _converged(item, digest) else 'no'} "
        f"| {_markdown(item.reason) or '-'} |"
        for item in targets
    ]
    if excluded:
        names = ", ".join(f"`{_markdown(name)}`" for name in excluded)
        lines += ["", f"対象外 (backend以外のイメージ): {names}"]
    _append(config.summary_file, lines)
    if failure_title:
        print(f"::error::{failure_title}")
        return exit_code
    print("Lambda image rollout completed")
    return SUCCESS


def _fail_globally(config: RolloutConfig, title: str, detail: str) -> int:
    _append(
        config.summary_file,
        [f"### {title}", "", "Result: **failure**", "", _markdown(detail)],
    )
    print(f"::error::{title}")
    return CONTRACT_FAILURE


def _append(path: Path, lines: Sequence[str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n\n")


def _sanitize(value: str) -> str:
    return _ACCOUNT_ID_RE.sub("<ACCOUNT_ID>", _ARN_RE.sub("<ARN>", value))


def _markdown(value: str) -> str:
    return (
        html.escape(_sanitize(value), quote=False)
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def _parse_args(argv: Sequence[str]) -> RolloutConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name-prefix", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    args = parser.parse_args(argv)

    if not _NAME_PREFIX_RE.fullmatch(args.name_prefix):
        raise RolloutInputError("name prefixの形式が不正")
    if not _REPOSITORY_RE.fullmatch(args.repository):
        raise RolloutInputError("repository名がECRの形式に一致しない")
    if not _IMAGE_TAG_RE.fullmatch(args.image_tag):
        raise RolloutInputError("image tagは40桁の小文字16進SHAが必要")
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        raise RolloutInputError("pollとtimeoutは正数が必要")

    return RolloutConfig(
        name_prefix=args.name_prefix,
        repository=args.repository,
        image_tag=args.image_tag,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        summary_file=args.summary_file,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint。"""

    try:
        config = _parse_args(argv if argv is not None else sys.argv[1:])
        aws_path = shutil.which("aws")
        if aws_path is None:
            raise RolloutInputError("aws CLIが見つからない")
    except RolloutInputError as exc:
        print(f"::error::{_sanitize(str(exc))}")
        return CONTRACT_FAILURE
    return rollout_lambda_images(config, AwsCliLambdaImageClient(aws_path))


if __name__ == "__main__":
    raise SystemExit(main())
