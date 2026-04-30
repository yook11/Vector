import { Header } from "@/components/layout/Header";
import { requireSession } from "@/lib/auth/guards";

export default async function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await requireSession();
  // Header は認証済 user 向けに `(protected)` 配下でのみ render する。auth page
  // (login/register) は root layout から直接 render されるため Header が出ない。
  // ラッパーの `mt-11` は固定 Header (h-11) の分の余白、`h-[calc(100dvh-2.75rem)]`
  // は子コンポーネントが viewport を満たすための高さ計算 (`2.75rem` = `h-11`)。
  return (
    <>
      <Header />
      <div className="mt-11 h-[calc(100dvh-2.75rem)]">{children}</div>
    </>
  );
}
