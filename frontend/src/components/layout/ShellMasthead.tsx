import { getProtectedNavItems } from "@/components/layout/nav-items";
import { SlimMasthead } from "@/components/layout/SlimMasthead";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { UserMenu } from "@/features/auth";
import { getCurrentSession } from "@/lib/auth/guards";
import { narrowRole } from "@/lib/auth/role";

interface ShellMastheadProps {
  activeHref: string;
}

/** 認証済み画面の紙面トップバー。session から nav を出し分け、SlimMasthead に
 *  共通スロットを束ねる。--vector-* トークン配下 (PaperSurface 内) で使う。 */
export async function ShellMasthead({ activeHref }: ShellMastheadProps) {
  const session = await getCurrentSession();
  const isAdmin = session !== null && narrowRole(session.user.role) === "admin";
  const navItems = getProtectedNavItems(isAdmin);

  return (
    <SlimMasthead
      navItems={navItems}
      activeHref={activeHref}
      themeSlot={<ThemeToggle />}
      userMenuSlot={
        <UserMenu
          compact
          buttonClassName="rounded-none text-[var(--vector-ink-muted)] hover:bg-transparent hover:text-[var(--vector-accent)]"
          emailClassName="text-[var(--vector-ink-muted)]"
        />
      }
    />
  );
}
