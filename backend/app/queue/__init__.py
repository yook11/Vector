"""taskiq 関連の技術関心を集約する layer。

broker / scheduler / lifecycle / Pure DI composition root / kiq message DTO /
全 cron schedule の SSoT を本 package 配下に閉じ込め、各 bounded context
(`collection/` `analysis/` `insights/` `audit/`) はドメイン責任
(Service / Repository / domain types / failure_handling) に純化する。

設計指針:
- Service はドメインの責任を語る。`.kiq()` (queue 依存) は task 側に置く
- 全 cron 表現は `schedule.py` の SSoT に集約 (minute 衝突確認の単一拠点)
- Trigger 等の kiq message DTO は `messages/` に分離 (Ready 型から taskiq
  都合の漏出を防ぐ)

詳細は plan: `~/.claude/plans/fizzy-bouncing-wilkes.md`。
"""
