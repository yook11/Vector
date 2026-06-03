"use client";

import { useUpdateSearchParams } from "@/lib/search-params/client";

interface PaperNewsPaginationProps {
  page: number;
  totalPages: number;
}

export function PaperNewsPagination({
  page,
  totalPages,
}: PaperNewsPaginationProps) {
  const updateSearchParams = useUpdateSearchParams();

  if (totalPages <= 1) return null;

  function goToPage(nextPage: number) {
    updateSearchParams({ page: nextPage <= 1 ? undefined : String(nextPage) });
  }

  return (
    <nav
      aria-label="ニュースページ"
      className="flex items-center justify-center gap-5 pt-8 pb-2 text-[12px] tracking-[0.12em] text-[var(--vector-ink-muted)]"
      style={{ fontFamily: "var(--font-vector-display)" }}
    >
      <button
        type="button"
        disabled={page <= 1}
        onClick={() => goToPage(page - 1)}
        className="border-b border-[var(--vector-line)] pb-1 uppercase disabled:cursor-not-allowed disabled:opacity-35"
      >
        Previous
      </button>
      <span className="tabular-nums">
        {page} / {totalPages}
      </span>
      <button
        type="button"
        disabled={page >= totalPages}
        onClick={() => goToPage(page + 1)}
        className="border-b border-[var(--vector-line)] pb-1 uppercase disabled:cursor-not-allowed disabled:opacity-35"
      >
        Next
      </button>
    </nav>
  );
}
