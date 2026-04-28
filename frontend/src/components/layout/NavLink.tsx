"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils/cn";

/**
 * Header / MobileNav から使う navigation Link。現在パスと一致したら
 * `aria-current="page"` を付与する。Server Component (Header) を Client 化
 * しなくても済むよう、Link 単体だけ Client 境界に切り出している。
 *
 * パス比較は完全一致 + サブパス前方一致 (`/news/123` で `/` だけが当たって
 * しまうのを避けるため、ルート `"/"` は完全一致のみ)。
 */
interface NavLinkProps extends Omit<ComponentProps<typeof Link>, "href"> {
  href: string;
  children: React.ReactNode;
}

export function NavLink({ href, className, children, ...rest }: NavLinkProps) {
  const pathname = usePathname();
  const isActive =
    href === "/"
      ? pathname === "/"
      : pathname === href || pathname?.startsWith(`${href}/`);

  return (
    <Link
      href={href}
      aria-current={isActive ? "page" : undefined}
      className={cn(className, isActive && "text-foreground")}
      {...rest}
    >
      {children}
    </Link>
  );
}
