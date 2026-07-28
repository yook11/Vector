# egress proxy。NAT Gateway を置かない代わりに、VPC から外へ出る唯一の経路になる。
#
# 設定は image に焼かない。段ごとの allowlist は subnet CIDR に依存し、それを
# 知っているのは Terraform だから。起動時に環境変数 SQUID_CONF から書き出す。
FROM alpine:3.22

RUN apk add --no-cache squid \
    && mkdir -p /var/cache/squid \
    && chown -R squid:squid /var/cache/squid

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER squid
EXPOSE 3128

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
