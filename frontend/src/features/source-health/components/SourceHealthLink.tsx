import { PendingAwareLink } from "@/components/layout/PageNavigation";

/**
 * Source Health 画面 (/admin/source-health) への admin 専用導線リンク。
 *
 * settings の admin page からのみ参照される小さな presentational link。href を
 * 単体テストで固定できるよう独立 component に切り出している。
 */
export function SourceHealthLink() {
  return (
    <PendingAwareLink
      href="/admin/source-health"
      className="inline-block text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground"
    >
      Source Health
    </PendingAwareLink>
  );
}
