"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useUpdateSearchParams } from "@/lib/search-params/client";
import { WINDOW_OPTIONS, type WindowOption } from "../window";

interface SourceHealthWindowSelectProps {
  current: WindowOption;
}

/**
 * 表示窓 (24h/48h/72h/7d) を切り替える client control。選択値を URL の `window`
 * search param に反映し、Server Component 側で再 fetch させる。
 */
export function SourceHealthWindowSelect({
  current,
}: SourceHealthWindowSelectProps) {
  const updateSearchParams = useUpdateSearchParams();

  return (
    <Select
      value={current}
      onValueChange={(value) => updateSearchParams({ window: value })}
    >
      <SelectTrigger
        className="h-9 w-[120px] text-xs border-border"
        aria-label="表示期間"
      >
        <SelectValue placeholder="Window" />
      </SelectTrigger>
      <SelectContent>
        {WINDOW_OPTIONS.map((opt) => (
          <SelectItem key={opt} value={opt}>
            {opt}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
