import Link from "next/link";
import { MobileNav } from "@/components/layout/MobileNav";
import { NavLink } from "@/components/layout/NavLink";
import { NavPendingDot } from "@/components/layout/NavPendingDot";
import { getProtectedNavItems, NAV_ICONS } from "@/components/layout/nav-items";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { UserMenu } from "@/features/auth";
import { getCurrentSession } from "@/lib/auth/guards";
import { narrowRole } from "@/lib/auth/role";

export async function Header() {
  const session = await getCurrentSession();
  const isAdmin = session !== null && narrowRole(session.user.role) === "admin";
  const navItems = getProtectedNavItems(isAdmin);

  return (
    <header className="fixed top-0 z-50 w-full bg-background/70 backdrop-blur-xl">
      <div className="mx-auto grid h-11 grid-cols-[1fr_auto_1fr] items-center px-5 sm:px-8">
        <div className="flex items-center">
          <Link
            href="/"
            className="text-sm font-semibold tracking-tight opacity-90 transition-opacity hover:opacity-100"
          >
            Vector
          </Link>
        </div>

        <nav className="hidden sm:flex items-center gap-7">
          {navItems.map((item) => {
            const Icon = NAV_ICONS[item.icon];
            return (
              <NavLink
                key={item.href}
                href={item.href}
                className="inline-flex items-center gap-1.5 text-xs text-foreground/60 transition-colors duration-300 hover:text-foreground"
              >
                <Icon aria-hidden="true" className="size-3.5" />
                {item.label}
                <NavPendingDot />
              </NavLink>
            );
          })}
        </nav>

        <div className="flex items-center justify-end gap-1">
          <ThemeToggle />
          <UserMenu />
          <MobileNav items={navItems} />
        </div>
      </div>
    </header>
  );
}
