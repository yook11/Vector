#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <aws-profile>" >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 64
fi

profile="$1"
expected_role=""
expected_regex=""
display_role=""
login_profile=""

case "$profile" in
  default)
    expected_regex='^AWSReservedSSO_ReadOnly_[[:xdigit:]]+$'
    display_role='AWSReservedSSO_ReadOnly_<SUFFIX>'
    login_profile='default'
    ;;
  vector-deploy)
    expected_regex='^AWSReservedSSO_VectorDeploy_[[:xdigit:]]+$'
    display_role='AWSReservedSSO_VectorDeploy_<SUFFIX>'
    login_profile='vector-deploy'
    ;;
  vector-plan)
    expected_role='vector-ci-terraform-plan'
    display_role="$expected_role"
    login_profile='vector-deploy'
    ;;
  vector-push)
    expected_role='vector-ci-app-push'
    display_role="$expected_role"
    login_profile='vector-deploy'
    ;;
  vector-apply | vector-rollout | vector-migrate)
    echo "本番apply・migration・rolloutは専用GitHub Actionsの承認後jobからのみ実行できます。" >&2
    exit 64
    ;;
  vector-admin)
    expected_regex='^AWSReservedSSO_WorkloadAdministrator_[[:xdigit:]]+$'
    display_role='AWSReservedSSO_WorkloadAdministrator_<SUFFIX>'
    login_profile='vector-admin'
    ;;
  *)
    echo "未対応のAWS profileが指定されました。" >&2
    usage
    exit 64
    ;;
esac

credential_env_names=(
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_SESSION_TOKEN
  AWS_SECURITY_TOKEN
  AWS_WEB_IDENTITY_TOKEN_FILE
  AWS_ROLE_ARN
  AWS_CONTAINER_CREDENTIALS_FULL_URI
  AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
  AWS_CONTAINER_AUTHORIZATION_TOKEN
  AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE
)

for name in "${credential_env_names[@]}"; do
  if [ -n "${!name+x}" ]; then
    echo "AWS credential環境変数が設定されているため停止します: $name" >&2
    echo "値を表示せず、現在のshellから明示的に解除してください。" >&2
    exit 1
  fi
done

for name in AWS_PROFILE AWS_DEFAULT_PROFILE; do
  value="${!name-}"
  if [ -n "$value" ] && [ "$value" != "$profile" ]; then
    echo "$name が検証対象profileと一致しません。" >&2
    exit 1
  fi
done

aws_cli="$(type -P aws || true)"
if [ -z "$aws_cli" ]; then
  echo "AWS CLIが見つかりません。" >&2
  exit 1
fi

profiles="$("$aws_cli" configure list-profiles 2>/dev/null || true)"
if ! grep -Fxq "$profile" <<<"$profiles"; then
  echo "AWS profile $profile がローカル設定に存在しません。" >&2
  exit 1
fi

case "$profile" in
  default | vector-deploy | vector-admin)
    expected_account="$(
      "$aws_cli" configure get sso_account_id --profile "$profile" 2>/dev/null || true
    )"
    ;;
  *)
    configured_role_arn="$(
      "$aws_cli" configure get role_arn --profile "$profile" 2>/dev/null || true
    )"
    IFS=: read -r _ _ _ _ expected_account _ <<<"$configured_role_arn"
    ;;
esac

if ! [[ "$expected_account" =~ ^[0-9]{12}$ ]]; then
  echo "AWS profile $profile のaccount設定を解決できません。" >&2
  exit 1
fi

set +e
caller_identity="$(
  AWS_PROFILE="$profile" AWS_DEFAULT_PROFILE="$profile" \
    "$aws_cli" sts get-caller-identity --query '[Account,Arn]' --output text 2>&1
)"
status=$?
set -e

if [ "$status" -ne 0 ]; then
  echo "AWS profile $profile の認証またはcaller確認に失敗しました。" >&2
  echo "自動loginは行いません。次を実行してから再確認してください。" >&2
  echo "aws sso login --profile $login_profile" >&2
  exit 1
fi

read -r caller_account caller_arn <<<"$caller_identity"
if [ "$caller_account" != "$expected_account" ]; then
  echo "AWS profile $profile のcaller accountが設定と一致しません。" >&2
  echo "自動assumeや権限変更は行いません。" >&2
  exit 1
fi

case "$caller_arn" in
  arn:*:sts::*:assumed-role/*/*)
    role_and_session="${caller_arn#*:assumed-role/}"
    caller_role="${role_and_session%%/*}"
    ;;
  *)
    echo "AWS profile $profile のcaller ARNが想定形式ではありません。" >&2
    exit 1
    ;;
esac

matches=false
if [ -n "$expected_role" ] && [ "$caller_role" = "$expected_role" ]; then
  matches=true
elif [ -n "$expected_regex" ] && [[ "$caller_role" =~ $expected_regex ]]; then
  matches=true
fi

if [ "$matches" != true ]; then
  echo "AWS profile $profile のcaller roleが期待値と一致しません。" >&2
  echo "期待role: $display_role" >&2
  echo "自動assumeや権限変更は行いません。" >&2
  echo "identityを確認し、必要なら次を実行してください。" >&2
  echo "aws sso login --profile $login_profile" >&2
  exit 1
fi

echo "AWS profile $profile: caller role $display_role"
