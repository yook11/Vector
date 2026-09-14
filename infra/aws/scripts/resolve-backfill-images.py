"""工程別backfillのイメージと定期起動状態をstateから引き継ぐ。"""

import argparse
import json
import re
import sys

STAGES = ("curation", "assessment", "embedding")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def current_instances(state: dict, resource_type: str) -> dict[str, dict]:
    if not isinstance(state, dict) or not isinstance(state.get("resources"), list):
        raise ValueError("有効なTerraform stateが必要です")
    current = {}
    for resource in state["resources"]:
        if (
            not isinstance(resource, dict)
            or resource.get("mode") not in ("managed", "data")
            or any(
                not isinstance(resource.get(field), str) or not resource[field]
                for field in ("type", "name")
            )
            or not isinstance(resource.get("instances"), list)
            or ("module" in resource and not isinstance(resource["module"], str))
        ):
            raise ValueError("stateのリソース構造が不正です")
        if (
            resource.get("module")
            or resource["mode"] != "managed"
            or resource["type"] != resource_type
            or resource["name"] != "backfill"
        ):
            continue
        for instance in resource["instances"]:
            if not isinstance(instance, dict) or (
                "deposed" in instance and not isinstance(instance["deposed"], str)
            ):
                raise ValueError("stateのインスタンス構造が不正です")
            if instance.get("deposed"):
                continue
            stage = instance.get("index_key")
            if stage not in STAGES or stage in current:
                raise ValueError("stateの工程が不正または重複しています")
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict):
                raise ValueError("stateの属性が不正です")
            current[stage] = attributes
    return current


def resolve(state: dict, args: argparse.Namespace) -> dict:
    functions = current_instances(state, "aws_lambda_function")
    schedules = current_instances(state, "aws_scheduler_schedule")
    result = {}
    for stage in STAGES:
        current_digest = None
        if stage in functions:
            image = functions[stage].get("image_uri")
            if not isinstance(image, str):
                raise ValueError("stateのLambdaイメージが不正です")
            repository, separator, current_digest = image.partition("@")
            if not repository or not separator or not DIGEST.fullmatch(current_digest):
                raise ValueError("stateのLambdaイメージはdigest形式ではありません")
        current_enabled = None
        if stage in schedules:
            value = schedules[stage].get("state")
            if value not in ("ENABLED", "DISABLED") or current_digest is None:
                raise ValueError("stateのScheduler稼働状態または対応Lambdaが不正です")
            current_enabled = value == "ENABLED"
        requested_digest = getattr(args, f"{stage}_digest")
        if requested_digest and not DIGEST.fullmatch(requested_digest):
            raise ValueError("指定イメージはsha256 digestでなければなりません")
        digest = requested_digest or current_digest
        requested_state = getattr(args, f"{stage}_state")
        if requested_state == "keep":
            enabled = current_enabled if current_enabled is not None else bool(digest)
        else:
            enabled = requested_state == "enabled"
        if enabled and not digest:
            raise ValueError("有効化にはLambdaのイメージが必要です")
        result[f"{stage}_backfill_image_digest"] = digest
        result[f"{stage}_backfill_enabled"] = enabled
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for stage in STAGES:
        parser.add_argument(f"--{stage}-digest", default="")
        parser.add_argument(
            f"--{stage}-state", choices=("keep", "enabled", "disabled"), default="keep"
        )
    args = parser.parse_args()
    try:
        result = resolve(json.load(sys.stdin), args)
    except (ValueError, TypeError):
        parser.exit(1, "backfillのstateまたは指定値が不正なため設定を生成できません\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
