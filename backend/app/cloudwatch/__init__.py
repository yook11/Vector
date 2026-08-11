"""CloudWatch を sink とする観測 helper 群 (app/logfire/ と並ぶ第 2 の sink)。

- emf: Embedded Metric Format の stdout emit (awslogs 経由でメトリクス自動抽出)

emit するのは CloudWatch 側に alarm / dashboard という consumer が確定している
メトリクスだけに絞る (specs/observability/cloudwatch-alerting.md)。
"""
