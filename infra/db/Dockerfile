# 同じPG17向けのpgvectorを使い、本体のマイナーバージョンを本番に固定する。
FROM pgvector/pgvector:0.8.2-pg17-bookworm@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966 AS pgvector

FROM postgres:17.9-bookworm@sha256:47f917f7409eacd22fc5dfb1dee634e1b55cf0c01d1a7eb701be2227a03e0641
COPY --from=pgvector /usr/lib/postgresql/17/lib/vector.so /usr/lib/postgresql/17/lib/vector.so
COPY --from=pgvector /usr/lib/postgresql/17/lib/bitcode/vector /usr/lib/postgresql/17/lib/bitcode/vector
COPY --from=pgvector /usr/lib/postgresql/17/lib/bitcode/vector.index.bc /usr/lib/postgresql/17/lib/bitcode/vector.index.bc
COPY --from=pgvector /usr/share/postgresql/17/extension/vector* /usr/share/postgresql/17/extension/
COPY --from=pgvector /usr/share/doc/pgvector /usr/share/doc/pgvector
