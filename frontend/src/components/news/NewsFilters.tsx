"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function NewsFilters() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const updateParam = useCallback(
    (key: string, value: string | undefined) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value) {
        params.set(key, value);
      } else {
        params.delete(key);
      }
      params.delete("page");
      router.push(`/?${params.toString()}`);
    },
    [router, searchParams],
  );

  return (
    <div className="flex flex-wrap gap-3">
      <Select
        value={searchParams.get("sentiment") ?? ""}
        onValueChange={(v) =>
          updateParam("sentiment", v === "all" ? undefined : v)
        }
      >
        <SelectTrigger className="w-[140px]">
          <SelectValue placeholder="Sentiment" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All</SelectItem>
          <SelectItem value="positive">Positive</SelectItem>
          <SelectItem value="negative">Negative</SelectItem>
          <SelectItem value="neutral">Neutral</SelectItem>
        </SelectContent>
      </Select>

      <Select
        value={searchParams.get("sortBy") ?? ""}
        onValueChange={(v) =>
          updateParam("sortBy", v === "default" ? undefined : v)
        }
      >
        <SelectTrigger className="w-[160px]">
          <SelectValue placeholder="Sort by" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="default">Latest</SelectItem>
          <SelectItem value="impactScore">Impact Score</SelectItem>
        </SelectContent>
      </Select>

      <Select
        value={searchParams.get("sortOrder") ?? ""}
        onValueChange={(v) =>
          updateParam("sortOrder", v === "default" ? undefined : v)
        }
      >
        <SelectTrigger className="w-[130px]">
          <SelectValue placeholder="Order" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="default">Desc</SelectItem>
          <SelectItem value="asc">Asc</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}
