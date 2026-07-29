# egress proxy。app 段が VPC から外へ出る唯一の経路になる。
#
# 設定は image に焼かない。段ごとの allowlist は subnet CIDR に依存し、それを
# 知っているのは Terraform だから。起動時に環境変数 SQUID_CONF から書き出す。
FROM alpine:3.22

# version を固定する。image tag (var.proxy_image_tag = "6.12-1") が squid の
# version を名乗るので、pin しないと alpine 側の bump で tag が嘘になる。
RUN apk add --no-cache squid=6.12-r0 \
    && mkdir -p /var/cache/squid \
    && chown -R squid:squid /var/cache/squid

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER squid
EXPOSE 3128

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
