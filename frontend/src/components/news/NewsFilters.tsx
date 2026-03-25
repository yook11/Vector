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
import type { NewsSourceResponse } from "@/types";

interface NewsFiltersProps {
  sources?: NewsSourceResponse[];
}

export function NewsFilters({ sources }: NewsFiltersProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const updateParam = useCallback(
    (key: string, value: string | undefined) => {
      const params = new URLSearchParams(searchParams?.toString() ?? "");
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
        value={searchParams?.get("impactLevel") ?? ""}
        onValueChange={(v) =>
          updateParam("impactLevel", v === "all" ? undefined : v)
        }
      >
        <SelectTrigger className="w-[160px]">
          <SelectValue placeholder="Impact Level" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All</SelectItem>
          <SelectItem value="low">Low+</SelectItem>
          <SelectItem value="medium">Medium+</SelectItem>
          <SelectItem value="high">High+</SelectItem>
          <SelectItem value="critical">Critical</SelectItem>
        </SelectContent>
      </Select>

      <Select
        value={searchParams?.get("sortBy") ?? ""}
        onValueChange={(v) =>
          updateParam("sortBy", v === "default" ? undefined : v)
        }
      >
        <SelectTrigger className="w-[160px]">
          <SelectValue placeholder="Sort by" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="default">Latest</SelectItem>
          <SelectItem value="impactLevel">Impact Level</SelectItem>
        </SelectContent>
      </Select>

      {sources && sources.length > 0 && (
        <Select
          value={searchParams?.get("sourceId") ?? ""}
          onValueChange={(v) =>
            updateParam("sourceId", v === "all" ? undefined : v)
          }
        >
          <SelectTrigger className="w-[160px]">
            <SelectValue placeholder="Source" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Sources</SelectItem>
            {sources.map((src) => (
              <SelectItem key={src.id} value={String(src.id)}>
                {src.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}

      <Select
        value={searchParams?.get("sortOrder") ?? ""}
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

      <Select
        value={searchParams?.get("perPage") ?? "12"}
        onValueChange={(v) => updateParam("perPage", v)}
      >
        <SelectTrigger className="w-[130px]">
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
