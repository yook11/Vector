"use client";

import { Loader2Icon } from "lucide-react";
import { useLinkStatus } from "next/link";
import { cn } from "@/lib/utils/cn";

interface NavPendingDotProps {
  className?: string;
}

/**
 * 祖先 <Link> のナビが pending の間だけスピナーを点灯させるインライン表示。
 * useLinkStatus は Link の子孫でのみ機能する。レイアウトシフトを避けるため
 * 常時固定サイズで描画し、opacity だけを切り替える。
 */
export function NavPendingDot({ className }: NavPendingDotProps) {
  const { pending } = useLinkStatus();

  return (
    <span
      aria-hidden="true"
      data-pending={pending ? "" : undefined}
      className={cn(
        "inline-flex size-3 shrink-0 items-center justify-center text-[var(--vector-accent)] transition-opacity duration-200",
        pending ? "opacity-100" : "opacity-0",
        className,
      )}
    >
      <Loader2Icon className="size-3 animate-spin" />
    </span>
  );
}
