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
import { DEFAULT_PER_PAGE, isPerPageOption } from "../per-page";
import { PerPageSelect } from "./PerPageSelect";

export function NewsFilters() {
  // SearchBar と同じく <Suspense> 配下なので非 null。空フォールバックで型を確定。
  const searchParams = useSearchParams() ?? new URLSearchParams();
  const updateSearchParams = useUpdateSearchParams();
  const rawSortOrder = searchParams.get("sortOrder");
  const sortOrderValue = rawSortOrder === "asc" ? "asc" : "default";
  const rawPerPage = searchParams.get("perPage");
  const perPageValue =
    rawPerPage && isPerPageOption(rawPerPage) ? rawPerPage : DEFAULT_PER_PAGE;

  return (
    <div className="flex flex-wrap items-center gap-2.5">
      <Select
        value={sortOrderValue}
        onValueChange={(v) =>
          updateSearchParams({
            sortOrder: v === "default" ? undefined : v,
            page: undefined,
          })
        }
      >
        <SelectTrigger
          className="h-9 w-[100px] text-xs border-border"
          aria-label="並び順"
        >
          <SelectValue placeholder="Order" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="default">Desc</SelectItem>
          <SelectItem value="asc">Asc</SelectItem>
        </SelectContent>
      </Select>

      <PerPageSelect current={perPageValue} />
    </div>
  );
}
