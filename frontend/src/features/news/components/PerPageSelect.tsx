"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useUpdateSearchParams } from "@/lib/search-params/client";
import { PER_PAGE_OPTIONS, type PerPageOption } from "../per-page";

interface PerPageSelectProps {
  current: PerPageOption;
}

export function PerPageSelect({ current }: PerPageSelectProps) {
  const { updateSearchParams, isPending } = useUpdateSearchParams();

  return (
    <Select
      value={current}
      disabled={isPending}
      onValueChange={(v) => updateSearchParams({ perPage: v, page: undefined })}
    >
      <SelectTrigger
        className="h-9 w-[100px] text-xs border-border"
        aria-label="1ページあたりの件数"
      >
        <SelectValue placeholder="Per page" />
      </SelectTrigger>
      <SelectContent>
        {PER_PAGE_OPTIONS.map((opt) => (
          <SelectItem key={opt} value={opt}>
            {opt} / page
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
