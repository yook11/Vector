"""認証カウンター掃除Lambdaのイメージと定期起動状態をstateから引き継ぐ。"""

import argparse
import json
import re
import sys

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def current_attributes(state: dict, resource_type: str) -> dict | None:
    if not isinstance(state, dict) or not isinstance(state.get("resources"), list):
        raise ValueError("有効なTerraform stateが必要です")
    matches = []
    for resource in state["resources"]:
        if not isinstance(resource, dict) or not isinstance(
            resource.get("instances"), list
        ):
            raise ValueError("stateのリソース構造が不正です")
        if (
            resource.get("module")
            or resource.get("mode") != "managed"
            or resource.get("type") != resource_type
            or resource.get("name") != "auth_rate_limit_cleanup"
        ):
            continue
        for instance in resource["instances"]:
            if not isinstance(instance, dict):
                raise ValueError("stateのインスタンス構造が不正です")
            if instance.get("deposed"):
                continue
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict):
                raise ValueError("stateの属性が不正です")
            matches.append(attributes)
    if len(matches) > 1:
        raise ValueError("stateの現行インスタンスが重複しています")
    return matches[0] if matches else None


def resolve(state: dict, digest: str, requested_state: str) -> dict:
    function = current_attributes(state, "aws_lambda_function")
    schedule = current_attributes(state, "aws_scheduler_schedule")
    current_digest = None
    if function is not None:
        image = function.get("image_uri")
        if not isinstance(image, str):
            raise ValueError("stateのLambdaイメージが不正です")
        repository, separator, current_digest = image.partition("@")
        if not repository or not separator or not DIGEST.fullmatch(current_digest):
            raise ValueError("stateのLambdaイメージはdigest形式ではありません")
    if digest and not DIGEST.fullmatch(digest):
        raise ValueError("指定イメージはsha256 digestでなければなりません")
    resolved_digest = digest or current_digest

    current_enabled = None
    if schedule is not None:
        value = schedule.get("state")
        if value not in ("ENABLED", "DISABLED") or current_digest is None:
            raise ValueError("stateのScheduler稼働状態または対応Lambdaが不正です")
        current_enabled = value == "ENABLED"
    if requested_state == "keep":
        enabled = current_enabled if current_enabled is not None else False
    else:
        enabled = requested_state == "enabled"
    if enabled and not resolved_digest:
        raise ValueError("有効化にはLambdaのイメージが必要です")
    return {
        "auth_rate_limit_cleanup_image_digest": resolved_digest,
        "auth_rate_limit_cleanup_enabled": enabled,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", default="")
    parser.add_argument(
        "--state", choices=("keep", "enabled", "disabled"), default="keep"
    )
    args = parser.parse_args()
    try:
        result = resolve(json.load(sys.stdin), args.digest, args.state)
    except (ValueError, TypeError):
        parser.exit(
            1, "認証カウンター掃除のstateまたは指定値が不正なため設定を生成できません\n"
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
