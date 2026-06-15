import Image from "next/image";
import Link from "next/link";
import type { ReactNode } from "react";
import { MobileNav } from "@/components/layout/MobileNav";
import {
  NAV_ICONS,
  type ProtectedNavItem,
} from "@/components/layout/nav-items";

interface SlimMastheadProps {
  navItems: ProtectedNavItem[];
  activeHref: string;
  themeSlot: ReactNode;
  userMenuSlot: ReactNode;
}

/** 詳細ページ用のスリムな紙面トップバー (sticky)。--vector-* トークン配下で使う。 */
export function SlimMasthead({
  navItems,
  activeHref,
  themeSlot,
  userMenuSlot,
}: SlimMastheadProps) {
  return (
    <header className="sticky top-0 z-50 border-b border-[var(--vector-line)] bg-[color-mix(in_oklab,var(--vector-paper)_86%,transparent)] backdrop-blur-[10px]">
      <div className="mx-auto flex h-[58px] max-w-[1180px] items-center gap-6 px-5 sm:px-8 lg:px-10">
        <Link
          href="/"
          aria-label="Vector ニュースへ"
          className="flex shrink-0 items-center gap-2.5"
        >
          <Image
            src="/icon.svg"
            alt=""
            width={28}
            height={28}
            className="size-7"
          />
          <span
            className="pl-[0.14em] text-[27px] font-bold leading-none tracking-[0.14em] text-[var(--vector-ink)]"
            style={{
              fontFamily: "var(--font-vector-wordmark), sans-serif",
            }}
          >
            VECTOR
          </span>
        </Link>

        <nav
          aria-label="主要ページ"
          className="hidden flex-1 items-center justify-center gap-6 md:flex"
          style={{ fontFamily: "var(--font-vector-maru)" }}
        >
          {navItems.map((item) => {
            const active = item.href === activeHref;
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
              </Link>
            );
          })}
        </nav>

        <div className="flex flex-1 items-center justify-end gap-2 sm:gap-3 md:flex-none">
          <div className="hidden lg:block">{userMenuSlot}</div>
          {themeSlot}
          <div className="md:hidden">
            <MobileNav
              items={navItems}
              triggerClassName="sm:inline-flex md:hidden"
            />
          </div>
        </div>
      </div>
    </header>
  );
}
