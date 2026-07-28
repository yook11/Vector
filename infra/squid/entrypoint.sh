#!/bin/sh
# SQUID_CONF が無ければ起動しない。設定漏れが「素通しの proxy」になるより、
# 起動しない方が安全 (fail-closed)。
set -eu

: "${SQUID_CONF:?SQUID_CONF is required}"

printf '%s' "$SQUID_CONF" > /tmp/squid.conf

# -N: フォアグラウンド (ECS がプロセスの生死を見る)
# -d1: ログを stderr へ (awslogs に流す)
exec squid -N -d1 -f /tmp/squid.conf
