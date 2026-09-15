"""取得Consumerの版と受信の稼働状態を安全に引き継ぐ。"""

import argparse
import json
import re
import sys

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def resolve(state: dict, requested_digest: str, requested_state: str) -> dict:
    if not isinstance(state, dict) or not isinstance(state.get("resources"), list):
        raise ValueError("有効なTerraform stateが必要です")
    images = []
    mappings = []
    for resource in state["resources"]:
        if (
            not isinstance(resource, dict)
            or resource.get("mode") not in ("managed", "data")
            or any(
                not isinstance(resource.get(k), str) or not resource[k]
                for k in ("type", "name")
            )
            or not isinstance(resource.get("instances"), list)
            or ("module" in resource and not isinstance(resource["module"], str))
        ):
            raise ValueError("stateのリソース構造が不正です")
        if (
            resource.get("module")
            or resource["mode"] != "managed"
            or resource["name"] != "acquisition_consumer"
            or resource["type"]
            not in ("aws_lambda_function", "aws_lambda_event_source_mapping")
        ):
            continue
        for instance in resource["instances"]:
            if not isinstance(instance, dict) or (
                "deposed" in instance and not isinstance(instance["deposed"], str)
            ):
                raise ValueError("stateのインスタンス構造が不正です")
            if instance.get("deposed"):
                continue
            attributes = instance.get("attributes")
            if not isinstance(attributes, dict):
                raise ValueError("stateの属性が不正です")
            if resource["type"] == "aws_lambda_function":
                image = attributes.get("image_uri")
                if not isinstance(image, str):
                    raise ValueError("stateのイメージが不正です")
                repository, separator, digest = image.partition("@")
                if not repository or not separator or not DIGEST.fullmatch(digest):
                    raise ValueError("stateのイメージはdigest形式で指定してください")
                images.append(digest)
            else:
                enabled = attributes.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("stateの受信状態が不正です")
                mappings.append(enabled)
    if len(images) > 1 or len(mappings) > 1 or (mappings and not images):
        raise ValueError("stateのLambdaと受信接続が不整合です")
    if requested_digest and not DIGEST.fullmatch(requested_digest):
        raise ValueError("指定イメージはsha256 digestで指定してください")
    digest = requested_digest or (images[0] if images else None)
    enabled = (
        next(iter(mappings), False)
        if requested_state == "keep"
        else requested_state == "enabled"
    )
    if enabled and not digest:
        raise ValueError("有効化にはイメージdigestが必要です")
    return {
        "acquisition_consumer_image_digest": digest,
        "acquisition_consumer_enabled": enabled,
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
            1, "取得Consumerのstateまたは指定値が不正なため設定を生成できません\n"
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
