"use client";

import { useSearchParams } from "next/navigation";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useUpdateSearchParams } from "@/lib/search-params/client";
import {
  DEFAULT_PER_PAGE,
  isPerPageOption,
  PER_PAGE_OPTIONS,
} from "../per-page";

export function PaperNewsControls() {
  const searchParams = useSearchParams() ?? new URLSearchParams();
  const updateSearchParams = useUpdateSearchParams();
  const rawSortOrder = searchParams.get("sortOrder");
  const sortOrderValue = rawSortOrder === "asc" ? "asc" : "desc";
  const rawPerPage = searchParams.get("perPage");
  const perPageValue =
    rawPerPage && isPerPageOption(rawPerPage) ? rawPerPage : DEFAULT_PER_PAGE;

  return (
    <div
      className="flex flex-wrap items-center justify-end gap-3 text-[12px] tracking-[0.06em] text-[var(--vector-ink-muted)]"
      style={{ fontFamily: "var(--font-vector-maru)" }}
    >
      <Select
        value={sortOrderValue}
        onValueChange={(value) =>
          updateSearchParams({
            sortOrder: value === "desc" ? undefined : value,
            page: undefined,
          })
        }
      >
        <SelectTrigger
          aria-label="並び順"
          className="h-8 w-[108px] rounded-none border-0 border-b border-[var(--vector-line)] bg-transparent px-0 text-[12px] tracking-[0.06em] text-[var(--vector-ink-muted)] shadow-none focus:ring-0"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="desc">新着順</SelectItem>
          <SelectItem value="asc">古い順</SelectItem>
        </SelectContent>
      </Select>

      <Select
        value={perPageValue}
        onValueChange={(value) =>
          updateSearchParams({
            perPage: value === DEFAULT_PER_PAGE ? undefined : value,
            page: undefined,
          })
        }
      >
        <SelectTrigger
          aria-label="表示件数"
          className="h-8 w-[130px] rounded-none border-0 border-b border-[var(--vector-line)] bg-transparent px-0 text-[12px] tracking-[0.06em] text-[var(--vector-ink-muted)] shadow-none focus:ring-0"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {PER_PAGE_OPTIONS.map((option) => (
            <SelectItem key={option} value={option}>
              {option}件 / ページ
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
