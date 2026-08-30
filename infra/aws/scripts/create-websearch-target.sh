#!/usr/bin/env bash
# AgentCore Gateway に web-search connector の target を作る。
#
# Terraform に入れられない理由: provider 6.62 の aws_bedrockagentcore_gateway_target
# は connector を持たず (target 種別は api_gateway / lambda / mcp_server /
# open_api_schema / smithy_model の 5 つ)、awscc にも gateway_target 資源が無い
# ため Cloud Control 経由の逃げ道も無い。API 側にだけ connector がある。
# provider が対応したら terraform import でこちらを畳む。
#
# 冪等。同名の target が既にあれば更新もせず終了する (connector の version を
# 上げるときは意図を明示させたいので、更新は手で消して作り直す)。
#
# 前提: AWS 資格情報が書き込み可能な profile で解決できること
# (既定 profile は読み取り専用なので AWS_PROFILE を明示する)。
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
NAME_PREFIX="${NAME_PREFIX:-vector}"
TARGET_NAME="${TARGET_NAME:-web-search}"
# 1.2.0 以降でのみ request 単位の domain / publishedDate フィルタが使える。
# time_filter.py が解決した期間をこのフィルタへ渡すので、下げると期間指定が死ぬ。
CONNECTOR_VERSION="${CONNECTOR_VERSION:-1.2.0}"

gateway_id="$(
  aws bedrock-agentcore-control list-gateways \
    --region "$REGION" \
    --query "items[?name=='${NAME_PREFIX}-web-search'].gatewayId | [0]" \
    --output text
)"

if [ -z "$gateway_id" ] || [ "$gateway_id" = "None" ]; then
  echo "gateway '${NAME_PREFIX}-web-search' が見つからない。先に terraform apply が要る。" >&2
  exit 1
fi

existing="$(
  aws bedrock-agentcore-control list-gateway-targets \
    --region "$REGION" \
    --gateway-identifier "$gateway_id" \
    --query "items[?name=='${TARGET_NAME}'].targetId | [0]" \
    --output text
)"

if [ -n "$existing" ] && [ "$existing" != "None" ]; then
  echo "target '${TARGET_NAME}' は既にある (targetId=${existing})。何もしない。"
  exit 0
fi

# parameterValues は空にする。target 単位の domain 固定リストは呼び出し側から
# 見えない絞り込みになるため、まずは request 単位のフィルタだけで運用して、
# 必要になってから足す (足す口はここ)。
#
# connector target は GATEWAY_IAM_ROLE 以外の credential provider を受け付けない。
# 省略すると作成に失敗するので明示する。
aws bedrock-agentcore-control create-gateway-target \
  --region "$REGION" \
  --gateway-identifier "$gateway_id" \
  --name "$TARGET_NAME" \
  --description "Managed web-search connector for the agent stage." \
  --credential-provider-configurations '[{"credentialProviderType": "GATEWAY_IAM_ROLE"}]' \
  --target-configuration "$(
    cat <<JSON
{
  "mcp": {
    "connector": {
      "source": {
        "connectorId": "web-search",
        "version": "${CONNECTOR_VERSION}"
      },
      "configurations": [
        {
          "name": "WebSearch",
          "parameterValues": {}
        }
      ]
    }
  }
}
JSON
  )"

echo "target '${TARGET_NAME}' を作成した (connector web-search ${CONNECTOR_VERSION})。"
