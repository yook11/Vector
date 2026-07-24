import Image from "next/image";
import type { ReactNode } from "react";
import { PendingAwareLink } from "@/components/layout/PageNavigation";

interface SlimMastheadProps {
  navSlot: ReactNode;
  mobileNavSlot: ReactNode;
  themeSlot: ReactNode;
  userMenuSlot: ReactNode;
}

/** 認証済み画面のスリムな紙面トップバー (sticky) の枠。session 非依存の static
 *  な器で、nav / user menu などの dynamic 部分は slot で受ける。--vector-* トー
 *  クン配下で使う。 */
export function SlimMasthead({
  navSlot,
  mobileNavSlot,
  themeSlot,
  userMenuSlot,
}: SlimMastheadProps) {
  return (
    <header className="sticky top-0 z-50 border-b border-[var(--vector-line)] bg-[color-mix(in_oklab,var(--vector-paper)_86%,transparent)] backdrop-blur-[10px]">
      <div className="mx-auto flex h-[58px] max-w-[1180px] items-center gap-6 px-5 sm:px-8 lg:px-10">
        <PendingAwareLink
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
        </PendingAwareLink>

        {navSlot}

        <div className="flex flex-1 items-center justify-end gap-2 sm:gap-3 md:flex-none">
          <div className="hidden lg:block">{userMenuSlot}</div>
          {themeSlot}
          <div className="md:hidden">{mobileNavSlot}</div>
        </div>
      </div>
    </header>
  );
}
