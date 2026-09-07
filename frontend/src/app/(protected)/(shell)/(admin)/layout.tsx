import { Header } from "@/components/layout/Header";
import { PageNavigationContent } from "@/components/layout/PageNavigation";
import { requireAdmin } from "@/lib/auth/guards";

// 各ページと変更操作も独立して管理者権限を確認する。
export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await requireAdmin();
  return (
    <>
      <Header />
      <PageNavigationContent className="mt-11 h-[calc(100dvh-2.75rem)]">
        {children}
      </PageNavigationContent>
    </>
  );
}
