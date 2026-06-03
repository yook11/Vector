import { Header } from "@/components/layout/Header";

export default function ProtectedShellLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // `/` は紙面マストヘッドにアプリ導線を統合するため、この shell の外に置く。
  // その他の認証済み画面は従来どおり固定ヘッダー分の余白と高さを確保する。
  return (
    <>
      <Header />
      <div className="mt-11 h-[calc(100dvh-2.75rem)]">{children}</div>
    </>
  );
}
