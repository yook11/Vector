"use client";

import { Button } from "@/components/ui/button";
import { useUpdateSearchParams } from "@/lib/search-params-client";

interface NewsPaginationProps {
  page: number;
  totalPages: number;
}

export function NewsPagination({ page, totalPages }: NewsPaginationProps) {
  const updateSearchParams = useUpdateSearchParams();

  if (totalPages <= 1) return null;

  function goToPage(p: number) {
    updateSearchParams({ page: p <= 1 ? undefined : String(p) });
  }

  return (
    <div className="flex items-center justify-center gap-3 pt-4 pb-2">
      <Button
        variant="outline"
        size="sm"
        disabled={page <= 1}
        onClick={() => goToPage(page - 1)}
        className="h-7 text-xs"
      >
        Previous
      </Button>
      <span className="text-xs text-muted-foreground tabular-nums">
        {page} / {totalPages}
      </span>
      <Button
        variant="outline"
        size="sm"
        disabled={page >= totalPages}
        onClick={() => goToPage(page + 1)}
        className="h-7 text-xs"
      >
        Next
      </Button>
    </div>
  );
}
