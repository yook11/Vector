import { ShellMobileNav } from "@/components/layout/ShellMobileNav";
import { ShellNav } from "@/components/layout/ShellNav";
import { SlimMasthead } from "@/components/layout/SlimMasthead";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { UserMenu } from "@/features/auth";

/** 認証済み画面の紙面トップバー。session 非依存の枠 (SlimMasthead) に、nav /
 *  UserMenu などの client island を差し込む。server で session を読まないため
 *  PPR の static shell に枠ごと載り、初回ロードの白画面を作らない。active 判定
 *  と admin 出し分けは client island 側 (ShellNav / UserMenu) が持つ。 */
export function ShellMasthead() {
  return (
    <SlimMasthead
      navSlot={<ShellNav />}
      mobileNavSlot={<ShellMobileNav />}
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
