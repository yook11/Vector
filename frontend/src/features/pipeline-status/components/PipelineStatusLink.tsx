import { PendingAwareLink } from "@/components/layout/PageNavigation";

/**
 * Pipeline Status 画面 (/admin/pipeline-status) への admin 専用導線リンク。
 *
 * settings の admin page からのみ参照される小さな presentational link。通常
 * Header の nav には載せず、一般ユーザー導線には出さない。href を単体テストで
 * 固定できるよう独立 component に切り出している。
 */
export function PipelineStatusLink() {
  return (
    <PendingAwareLink
      href="/admin/pipeline-status"
      className="inline-block text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground"
    >
      Pipeline Status
    </PendingAwareLink>
  );
}
