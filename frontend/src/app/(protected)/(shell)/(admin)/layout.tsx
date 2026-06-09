import { Header } from "@/components/layout/Header";
import { requireAdmin } from "@/lib/auth/guards";

// admin role を持つユーザのみ通す route segment。
// (protected) layout で session 自体は担保済みなので、ここでは role 判定だけ行う。
//
// 注意: layout は data fetch をブロックするが、同 segment 配下の Server Action や
// Route Handler は layout を経由せず実行される。admin 権限を要する mutation を追加する
// 場合は、各エントリポイントで `requireAdmin()` を **明示的に** 呼ぶこと。
//
// admin 画面 (settings / pipeline-status) は紙面化対象外のため、汎用固定ヘッダーと
// その高さ分の余白を確保する (旧 (shell) layout から移設)。
export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await requireAdmin();
  return (
    <>
      <Header />
      <div className="mt-11 h-[calc(100dvh-2.75rem)]">{children}</div>
    </>
  );
}
