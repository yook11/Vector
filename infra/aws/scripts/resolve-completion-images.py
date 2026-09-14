"""補完Lambdaの版と稼働状態を保持し、明示指定時だけ更新する。"""

import argparse
import json
import re
import sys


def resolve_digest(state: dict, resource_name: str, requested: str) -> str | None:
    if not isinstance(state, dict) or not isinstance(state.get("resources"), list):
        raise ValueError("有効なTerraform stateが必要です")
    images = []
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
            or resource["type"] != "aws_lambda_function"
            or resource["name"] != resource_name
        ):
            continue
        for instance in resource["instances"]:
            if not isinstance(instance, dict) or (
                "deposed" in instance and not isinstance(instance["deposed"], str)
            ):
                raise ValueError("stateのLambdaインスタンス構造が不正です")
            if instance.get("deposed"):
                continue
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict) or not isinstance(
                attributes.get("image_uri"), str
            ):
                raise ValueError("stateのLambdaイメージが不正です")
            images.append(attributes["image_uri"])
    if len(images) > 1:
        raise ValueError("Lambdaのstateに複数の現行イメージが存在する")
    current = None
    if images:
        _, separator, current = images[0].partition("@")
        if not separator or not re.fullmatch(r"sha256:[0-9a-f]{64}", current):
            raise ValueError("stateのLambdaイメージはdigest形式ではない")
    if requested and not re.fullmatch(r"sha256:[0-9a-f]{64}", requested):
        raise ValueError("指定するイメージはsha256 digestでなければならない")
    return requested or current


def resolve_enabled(
    state: dict, resource_type: str, resource_name: str, requested: str
) -> bool:
    values = []
    for resource in state["resources"]:
        if (
            resource.get("module")
            or resource["mode"] != "managed"
            or resource["type"] != resource_type
            or resource["name"] != resource_name
        ):
            continue
        for instance in resource["instances"]:
            if not isinstance(instance, dict) or (
                "deposed" in instance and not isinstance(instance["deposed"], str)
            ):
                raise ValueError("stateの稼働状態インスタンスが不正です")
            if instance.get("deposed"):
                continue
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict):
                raise ValueError("stateの稼働状態属性が不正です")
            if resource_type == "aws_scheduler_schedule":
                value = attributes.get("state")
                if value not in ("ENABLED", "DISABLED"):
                    raise ValueError("stateのScheduler稼働状態が不正です")
                values.append(value == "ENABLED")
            else:
                value = attributes.get("enabled")
                if not isinstance(value, bool):
                    raise ValueError("stateの受信稼働状態が不正です")
                values.append(value)
    if len(values) > 1:
        raise ValueError("stateに複数の現行稼働状態が存在します")
    if requested != "keep":
        return requested == "enabled"
    return values[0] if values else False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer-digest", default="")
    parser.add_argument("--relay-digest", default="")
    for name in ("consumer", "relay"):
        parser.add_argument(
            f"--{name}-state", choices=("keep", "enabled", "disabled"), default="keep"
        )
    args = parser.parse_args()
    try:
        state = json.load(sys.stdin)
        consumer = resolve_digest(state, "completion_consumer", args.consumer_digest)
        relay = resolve_digest(state, "completion_outbox_relay", args.relay_digest)
        consumer_enabled = resolve_enabled(
            state,
            "aws_lambda_event_source_mapping",
            "completion_consumer",
            args.consumer_state,
        )
        relay_enabled = resolve_enabled(
            state, "aws_scheduler_schedule", "completion_outbox_relay", args.relay_state
        )
        if (consumer_enabled and not consumer) or (relay_enabled and not relay):
            raise ValueError("有効化にはLambdaのイメージが必要です")
    except (ValueError, TypeError):
        parser.exit(1, "補完のstateまたは指定値が不正なため設定を生成できません\n")
    print(
        json.dumps(
            {
                "completion_consumer_image_digest": consumer,
                "completion_outbox_relay_image_digest": relay,
                "completion_consumer_enabled": consumer_enabled,
                "completion_relay_enabled": relay_enabled,
            }
        )
    )


if __name__ == "__main__":
    main()
