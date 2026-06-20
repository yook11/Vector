"use client";

import { Loader2Icon } from "lucide-react";
import { useUpdateSearchParams } from "@/lib/search-params/client";
import { cn } from "@/lib/utils/cn";

interface PaperNewsPaginationProps {
  page: number;
  totalPages: number;
}

export function PaperNewsPagination({
  page,
  totalPages,
}: PaperNewsPaginationProps) {
  const { updateSearchParams, isPending } = useUpdateSearchParams();

  if (totalPages <= 1) return null;

  function goToPage(nextPage: number) {
    updateSearchParams({ page: nextPage <= 1 ? undefined : String(nextPage) });
  }

  return (
    <nav
      aria-label="ニュースページ"
      aria-busy={isPending}
      className="flex items-center justify-center gap-5 pt-8 pb-2 text-[12px] tracking-[0.12em] text-[var(--vector-ink-muted)]"
      style={{ fontFamily: "var(--font-vector-display)" }}
    >
      <button
        type="button"
        disabled={page <= 1 || isPending}
        onClick={() => goToPage(page - 1)}
        className="border-b border-[var(--vector-line)] pb-1 uppercase disabled:cursor-not-allowed disabled:opacity-35"
      >
        Previous
      </button>
      <span className="inline-flex items-center gap-1.5 tabular-nums">
        {page} / {totalPages}
        <Loader2Icon
          aria-hidden="true"
          className={cn(
            "size-3 shrink-0 animate-spin transition-opacity duration-200",
            isPending ? "opacity-100" : "opacity-0",
          )}
        />
      </span>
      <button
        type="button"
        disabled={page >= totalPages || isPending}
        onClick={() => goToPage(page + 1)}
        className="border-b border-[var(--vector-line)] pb-1 uppercase disabled:cursor-not-allowed disabled:opacity-35"
      >
        Next
      </button>
    </nav>
  );
}
