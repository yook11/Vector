import Image from "next/image";
import Link from "next/link";
import { Fragment, type ReactNode } from "react";
import { MobileNav } from "@/components/layout/MobileNav";
import { NavPendingDot } from "@/components/layout/NavPendingDot";
import {
  NAV_ICONS,
  type ProtectedNavItem,
} from "@/components/layout/nav-items";
import type { ArticleQuery } from "@/types";
import type { CategoryDetail } from "@/types/types.gen";
import { buildDashboardCategoryHref } from "./paper-hrefs";

interface DashboardMastheadProps {
  activeCategory?: string;
  categories: CategoryDetail[];
  currentQuery: ArticleQuery;
  dateSlot: ReactNode;
  navItems: ProtectedNavItem[];
  themeSlot: ReactNode;
  userMenuSlot: ReactNode;
}

export function DashboardMasthead({
  activeCategory,
  categories,
  currentQuery,
  dateSlot,
  navItems,
  themeSlot,
  userMenuSlot,
}: DashboardMastheadProps) {
  const allHref = buildDashboardCategoryHref({
    query: currentQuery,
  });
  const isAll = activeCategory === undefined;
  // 凡例はバッジが1つも出ないとき (全カテゴリ recentCount 0/未設定) は説明対象が
  // 無いので隠す。CategoryNavLink のバッジ表示条件と揃える。
  const showCountLegend = categories.some(
    (category) => (category.recentCount ?? 0) > 0,
  );

  return (
    <header className="relative z-10 px-5 sm:px-8 lg:px-10">
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-4 pt-5 pb-3">
        <div aria-hidden="true" />
        <nav
          aria-label="主要ページ"
          className="hidden items-center justify-center md:inline-flex"
        >
          {navItems.map((item, index) => {
            const active = item.href === "/";
            const Icon = NAV_ICONS[item.icon];
            return (
              <Fragment key={item.href}>
                {index > 0 && (
                  <span
                    aria-hidden="true"
                    className="mx-[clamp(12px,1.8vw,20px)] h-[18px] w-px bg-[var(--vector-line)]"
                  />
                )}
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={
                    active
                      ? "inline-flex items-center gap-2 border-b-2 border-[var(--vector-accent)] pt-0.5 pb-[5px]"
                      : "inline-flex items-center gap-2 border-b-2 border-transparent pt-0.5 pb-[5px]"
                  }
                >
                  <Icon
                    aria-hidden="true"
                    className={
                      active
                        ? "size-4 text-[var(--vector-accent)]"
                        : "size-4 text-[var(--vector-ink-muted)] opacity-70"
                    }
                  />
                  <span
                    className={
                      active
                        ? "text-[15px] font-semibold tracking-[0.05em] text-[var(--vector-ink)]"
                        : "text-[15px] font-medium tracking-[0.05em] text-[var(--vector-ink-soft)] transition-colors hover:text-[var(--vector-ink)]"
                    }
                    style={{ fontFamily: "var(--font-vector-display)" }}
                  >
                    {item.label}
                  </span>
                </Link>
              </Fragment>
            );
          })}
        </nav>

        <div className="flex min-w-0 items-center justify-end gap-2 sm:gap-3">
          {dateSlot}
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
            width={64}
            height={64}
            className="size-12 sm:size-16"
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
          className="flex min-w-max items-center justify-center gap-1.5 md:min-w-0 md:flex-wrap"
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
          {showCountLegend && <CountLegend />}
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
    <Link
      href={href}
      prefetch={false}
      aria-current={active ? "page" : undefined}
      className={
        active
          ? "inline-flex items-center gap-2 whitespace-nowrap rounded-[9px] bg-[var(--vector-accent-tint)] px-[13px] py-2 text-[13px] font-bold tracking-[0.02em] text-[var(--vector-accent-ink)] shadow-[inset_0_-2px_0_var(--vector-accent)]"
          : "inline-flex items-center gap-2 whitespace-nowrap rounded-[9px] px-[13px] py-2 text-[13px] font-medium tracking-[0.02em] text-[var(--vector-ink-soft)] transition-colors hover:text-[var(--vector-ink)]"
      }
    >
      {label}
      {hasRecentCount && (
        <span
          className={
            active
              ? "inline-flex h-[18px] min-w-[19px] items-center justify-center rounded-full bg-[var(--vector-accent)] px-1.5 text-[11px] font-bold leading-none text-[var(--vector-on-accent)]"
              : "inline-flex h-[18px] min-w-[19px] items-center justify-center rounded-full bg-[var(--vector-accent-tint)] px-1.5 text-[11px] font-bold leading-none text-[var(--vector-accent-ink)]"
          }
          style={{ fontFamily: "var(--font-vector-sans)" }}
        >
          {recentCount}
        </span>
      )}
      <NavPendingDot />
    </Link>
  );
}

/** カテゴリ件数バッジの対応を示すインライン凡例 (legendPos: inlineEnd)。 */
function CountLegend() {
  return (
    <span className="ml-2 inline-flex shrink-0 items-center gap-2 rounded-full border border-[color-mix(in_oklab,var(--vector-accent)_35%,transparent)] bg-[var(--vector-accent-tint)] py-[5px] pr-[13px] pl-1.5">
      <span
        className="inline-flex h-[19px] min-w-[19px] items-center justify-center rounded-full bg-[var(--vector-accent)] px-[7px] text-[11px] font-bold tracking-[0.04em] text-[var(--vector-on-accent)]"
        style={{ fontFamily: "var(--font-vector-sans)" }}
      >
        24H
      </span>
      <span
        className="text-[11.5px] font-bold tracking-[0.06em] text-[var(--vector-accent-ink)]"
        style={{ fontFamily: "var(--font-vector-maru)" }}
      >
        の新着件数
      </span>
    </span>
  );
}
