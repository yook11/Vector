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

export function NewsFilters() {
  // SearchBar と同じく <Suspense> 配下なので非 null。空フォールバックで型を確定。
  const searchParams = useSearchParams() ?? new URLSearchParams();
  const updateSearchParams = useUpdateSearchParams();

  const updateParam = (key: string, value: string | undefined) => {
    updateSearchParams({ [key]: value, page: undefined });
  };

  return (
    <div className="flex flex-wrap items-center gap-2.5">
      <Select
        value={searchParams.get("sortOrder") ?? ""}
        onValueChange={(v) =>
          updateParam("sortOrder", v === "default" ? undefined : v)
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

      <Select
        value={searchParams.get("perPage") ?? "12"}
        onValueChange={(v) => updateParam("perPage", v)}
      >
        <SelectTrigger
          className="h-9 w-[100px] text-xs border-border"
          aria-label="1ページあたりの件数"
        >
          <SelectValue placeholder="Per page" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="12">12 / page</SelectItem>
          <SelectItem value="24">24 / page</SelectItem>
          <SelectItem value="48">48 / page</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}
