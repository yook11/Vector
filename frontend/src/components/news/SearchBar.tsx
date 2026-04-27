"use client";

import { Search, X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { useUpdateSearchParams } from "@/lib/search-params-client";

const DEBOUNCE_MS = 500;

export function SearchBar() {
  const searchParams = useSearchParams();
  const updateSearchParams = useUpdateSearchParams();
  const currentQ = searchParams?.get("q") ?? "";
  const [value, setValue] = useState(currentQ);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Sync local state when URL changes externally
  useEffect(() => {
    setValue(currentQ);
  }, [currentQ]);

  const navigate = useCallback(
    (q: string) => {
      const trimmed = q.trim();
      updateSearchParams({ q: trimmed || undefined, page: undefined });
    },
    [updateSearchParams],
  );

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const next = e.target.value;
    setValue(next);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => navigate(next), DEBOUNCE_MS);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      if (timerRef.current) clearTimeout(timerRef.current);
      navigate(value);
    }
  };

  const handleClear = () => {
    setValue("");
    if (timerRef.current) clearTimeout(timerRef.current);
    navigate("");
  };

  return (
    <div className="relative w-full sm:w-72 shrink-0">
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground" />
      <Input
        type="search"
        placeholder="Search articles…"
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        spellCheck={false}
        aria-label="Search articles"
        className="h-9 pl-9 pr-9 text-xs border-border"
      />
      {value && (
        <button
          type="button"
          onClick={handleClear}
          aria-label="Clear search"
          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}
