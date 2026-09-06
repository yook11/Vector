"""通常applyではLambdaのdigestを保持し、明示指定時だけ更新する。"""

import argparse
import json
import re
import sys


def resolve_digest(state: dict, requested: str) -> str | None:
    images = [
        instance["attributes"]["image_uri"]
        for resource in state["resources"]
        if resource.get("mode") == "managed"
        and resource["type"] == "aws_lambda_function"
        and resource["name"] == "outbox_relay"
        for instance in resource.get("instances", [])
        if not instance.get("deposed")
    ]
    if len(images) > 1:
        raise ValueError("relayのstateに複数の現行イメージが存在する")
    current = None
    if images:
        _, separator, current = images[0].partition("@")
        if not separator or not re.fullmatch(r"sha256:[0-9a-f]{64}", current):
            raise ValueError("stateのrelayイメージはdigest形式ではない")
    if requested and not re.fullmatch(r"sha256:[0-9a-f]{64}", requested):
        raise ValueError("指定するイメージはsha256 digestでなければならない")
    return requested or current


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", default="")
    args = parser.parse_args()
    state = json.load(sys.stdin)
    print(json.dumps({"outbox_relay_image_digest": resolve_digest(state, args.digest)}))
