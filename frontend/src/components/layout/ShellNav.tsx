"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NavPendingDot } from "@/components/layout/NavPendingDot";
import { NAV_ICONS } from "@/components/layout/nav-items";
import { useProtectedNavItems } from "@/components/layout/useProtectedNavItems";

/**
 * 認証済みシェルの desktop nav。active 判定を usePathname で client 側に持つため、
 * 共有 layout から activeHref を渡さずに済む (masthead を永続 layout へ載せる前提)。
 * 項目の出し分けは useProtectedNavItems が担う。
 */
export function ShellNav() {
  const navItems = useProtectedNavItems();
  const pathname = usePathname();

  return (
    <nav
      aria-label="主要ページ"
      className="hidden flex-1 items-center justify-center gap-6 md:flex"
      style={{ fontFamily: "var(--font-vector-maru)" }}
    >
      {navItems.map((item) => {
        const active =
          item.href === "/"
            ? pathname === "/"
            : pathname === item.href || pathname?.startsWith(`${item.href}/`);
        const Icon = NAV_ICONS[item.icon];
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={
              active
                ? "inline-flex items-center gap-1.5 border-b-2 border-[var(--vector-accent)] pb-0.5 text-[13px] font-bold tracking-[0.04em] text-[var(--vector-ink)]"
                : "inline-flex items-center gap-1.5 border-b-2 border-transparent pb-0.5 text-[13px] font-medium tracking-[0.04em] text-[var(--vector-ink-soft)] transition-colors hover:text-[var(--vector-ink)]"
            }
          >
            <Icon
              aria-hidden="true"
              className={
                active
                  ? "size-3.5 text-[var(--vector-accent)]"
                  : "size-3.5 text-[var(--vector-ink-muted)] opacity-70"
              }
            />
            {item.label}
            <NavPendingDot />
          </Link>
        );
      })}
    </nav>
  );
}
