"""通常applyではLambdaのdigestを保持し、明示指定時だけ更新する。"""

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer-digest", default="")
    parser.add_argument("--relay-digest", default="")
    args = parser.parse_args()
    state = json.load(sys.stdin)
    print(
        json.dumps(
            {
                "curation_consumer_image_digest": resolve_digest(
                    state, "curation_consumer", args.consumer_digest
                ),
                "curation_outbox_relay_image_digest": resolve_digest(
                    state, "curation_outbox_relay", args.relay_digest
                ),
            }
        )
    )
