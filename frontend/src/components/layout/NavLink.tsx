"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentProps } from "react";
import { PendingAwareLink } from "@/components/layout/PageNavigation";
import { cn } from "@/lib/utils/cn";

/**
 * Header / MobileNav から使う navigation Link。現在パスと一致したら
 * `aria-current="page"` を付与する。Server Component (Header) を Client 化
 * しなくても済むよう、Link 単体だけ Client 境界に切り出している。
 *
 * パス比較は完全一致 + サブパス前方一致 (`/news/123` で `/` だけが当たって
 * しまうのを避けるため、ルート `"/"` は完全一致のみ)。
 */
interface NavLinkProps
  extends Omit<ComponentProps<typeof PendingAwareLink>, "href"> {
  href: string;
  children: React.ReactNode;
  pendingAware?: boolean;
}

export function NavLink({
  href,
  className,
  children,
  pendingAware = false,
  ...rest
}: NavLinkProps) {
  const pathname = usePathname();
  const isActive =
    href === "/"
      ? pathname === "/"
      : pathname === href || pathname?.startsWith(`${href}/`);
  const ariaCurrent = isActive ? ("page" as const) : undefined;

  const linkProps = {
    href,
    "aria-current": ariaCurrent,
    className: cn(className, isActive && "text-foreground"),
    ...rest,
  };

  return pendingAware ? (
    <PendingAwareLink {...linkProps}>{children}</PendingAwareLink>
  ) : (
    <Link {...linkProps}>{children}</Link>
  );
}
