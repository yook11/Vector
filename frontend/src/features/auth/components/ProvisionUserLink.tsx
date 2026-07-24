import { PendingAwareLink } from "@/components/layout/PageNavigation";

export function ProvisionUserLink() {
  return (
    <PendingAwareLink
      href="/admin/users/new"
      className="inline-block text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground"
    >
      デモユーザーを登録
    </PendingAwareLink>
  );
}
