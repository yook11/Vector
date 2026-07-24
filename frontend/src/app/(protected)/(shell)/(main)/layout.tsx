import { PageNavigationContent } from "@/components/layout/PageNavigation";
import { ShellMasthead } from "@/components/layout/ShellMasthead";
import { PaperSurface, PaperTexture } from "@/components/paper";

/**
 * briefing / trends / watchlist 共有の紙面シェル。masthead を layout に載せる
 * ことで、これら sibling 間のナビゲーションで masthead を再マウントせず永続させ
 * る (本文だけ skeleton→stream)。masthead は session 非依存なので PPR の static
 * shell に枠ごと載る。
 *
 * (admin) は独自の Header を持つため (main) には入れない (二重ヘッダー回避)。
 */
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
