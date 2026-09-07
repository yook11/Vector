import { PageNavigationContent } from "@/components/layout/PageNavigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";

// ブリーフィング一覧とトレンド間の遷移で紙面ヘッダーを維持する。
export default function ShellMainLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <PaperSurface>
      <ShellMasthead />
      <div className="relative min-h-dvh w-full overflow-hidden">
        <PaperTexture />
        <PageNavigationContent>{children}</PageNavigationContent>
      </div>
    </PaperSurface>
  );
}
