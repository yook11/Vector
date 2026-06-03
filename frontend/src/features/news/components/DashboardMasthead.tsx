import Image from "next/image";
import Link from "next/link";
import type { ReactNode } from "react";
import { MobileNav } from "@/components/layout/MobileNav";
import type { ProtectedNavItem } from "@/components/layout/nav-items";
import type { ArticleQuery } from "@/types";
import type { CategoryDetail } from "@/types/types.gen";
import { buildDashboardCategoryHref } from "./paper-hrefs";

interface DashboardMastheadProps {
  activeCategory?: string;
  categories: CategoryDetail[];
  currentQuery: ArticleQuery;
  displayDate: string;
  navItems: ProtectedNavItem[];
  themeSlot: ReactNode;
  userMenuSlot: ReactNode;
}

export function DashboardMasthead({
  activeCategory,
  categories,
  currentQuery,
  displayDate,
  navItems,
  themeSlot,
  userMenuSlot,
}: DashboardMastheadProps) {
  const allHref = buildDashboardCategoryHref({
    query: currentQuery,
  });
  const isAll = activeCategory === undefined;

  return (
    <header className="relative z-10 px-5 sm:px-8 lg:px-10">
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-4 pt-5 pb-3">
        <div aria-hidden="true" />
        <nav
          aria-label="主要ページ"
          className="hidden items-center justify-center gap-6 md:flex"
          style={{ fontFamily: "var(--font-vector-maru)" }}
        >
          {navItems.map((item) => {
            const active = item.href === "/";
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={
                  active
                    ? "text-[12px] font-bold tracking-[0.12em] text-[var(--vector-accent-ink)]"
                    : "text-[12px] font-medium tracking-[0.12em] text-[var(--vector-ink-soft)] transition-colors hover:text-[var(--vector-ink)]"
                }
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="flex min-w-0 items-center justify-end gap-2 sm:gap-3">
          <span
            className="hidden shrink-0 text-[12.5px] italic tracking-[0.04em] text-[var(--vector-ink-muted)] sm:inline"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            {displayDate}
          </span>
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

      <div className="flex items-center justify-center gap-4 py-5 sm:gap-6">
        <span className="h-px flex-1 bg-[color-mix(in_oklab,var(--vector-ink)_20%,transparent)]" />
        <Link
          href="/"
          className="inline-flex items-center gap-3 sm:gap-4"
          aria-label="Vector ニュースへ"
        >
          <Image
            src="/icon.svg"
            alt=""
            width={44}
            height={44}
            className="size-9 rounded-[8px] sm:size-11"
          />
          <span
            className="inline-block pl-[0.12em] text-[56px] font-bold leading-none tracking-[0.12em] text-[var(--vector-ink)] [font-optical-sizing:auto] [font-stretch:condensed] [font-variation-settings:'opsz'_72] sm:text-[76px]"
            style={{
              fontFamily:
                "var(--font-vector-wordmark), 'Big Shoulders Display', sans-serif",
              transform: "scaleX(0.92)",
              transformOrigin: "center",
            }}
          >
            VECTOR
          </span>
        </Link>
        <span className="h-px flex-1 bg-[color-mix(in_oklab,var(--vector-ink)_20%,transparent)]" />
      </div>

      <div className="mb-3 border-t-[3px] border-double border-[var(--vector-ink)]" />

      <nav
        aria-label="ニュースカテゴリ"
        className="-mx-5 overflow-x-auto px-5 pb-5 [-ms-overflow-style:none] [scrollbar-width:none] sm:-mx-8 sm:px-8 lg:-mx-10 lg:px-10 [&::-webkit-scrollbar]:hidden"
      >
        <div
          className="flex min-w-max items-center justify-center gap-4 md:min-w-0 md:flex-wrap"
          style={{ fontFamily: "var(--font-vector-maru)" }}
        >
          <CategoryNavLink href={allHref} active={isAll} label="すべて" />
          {categories.map((category) => {
            const href = buildDashboardCategoryHref({
              category: category.slug,
              query: currentQuery,
            });
            const recentCountProps =
              category.recentCount !== undefined
                ? { recentCount: category.recentCount }
                : {};
            return (
              <CategoryNavLink
                key={category.slug}
                href={href}
                active={activeCategory === category.slug}
                label={category.name}
                {...recentCountProps}
              />
            );
          })}
        </div>
      </nav>
    </header>
  );
}

interface CategoryNavLinkProps {
  active: boolean;
  href: string;
  label: string;
  recentCount?: number;
}

function CategoryNavLink({
  active,
  href,
  label,
  recentCount,
}: CategoryNavLinkProps) {
  const hasRecentCount = recentCount !== undefined && recentCount > 0;

  return (
    <span className="inline-flex items-center gap-4">
      <Link
        href={href}
        aria-current={active ? "page" : undefined}
        className={
          active
            ? "whitespace-nowrap border-b-2 border-[var(--vector-accent)] pb-1 text-[12px] font-bold tracking-[0.1em] text-[var(--vector-accent-ink)]"
            : "whitespace-nowrap border-b-2 border-transparent pb-1 text-[12px] font-medium tracking-[0.1em] text-[var(--vector-ink-soft)] transition-colors hover:text-[var(--vector-ink)]"
        }
      >
        {label}
        {hasRecentCount && (
          <span className="ml-1.5 text-[10px] tracking-normal text-[var(--vector-ink-muted)]">
            +{recentCount}
          </span>
        )}
      </Link>
      <span aria-hidden="true" className="text-[var(--vector-line)]">
        ·
      </span>
    </span>
  );
}
