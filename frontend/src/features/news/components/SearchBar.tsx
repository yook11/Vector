"use client";

import { Search, X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { useUpdateSearchParams } from "@/lib/search-params/client";

const DEBOUNCE_MS = 500;
// 入力長 cap。URL に乗せる前提なので適度な上限を置く。
const MAX_QUERY_LENGTH = 200;

export function SearchBar() {
  // <Suspense> 配下の Client Component なので useSearchParams は実体を返すが、
  // 静的 prerender 経路の null を型で除くため空 URLSearchParams にフォールバック。
  const searchParams = useSearchParams() ?? new URLSearchParams();
  const updateSearchParams = useUpdateSearchParams();
  const currentQ = searchParams.get("q") ?? "";
  const [value, setValue] = useState(currentQ);
  // URL の q が外部変更 (browser back / direct navigation) で変わったとき
  // input value をそれに追従させる。useEffect での同期は React 公式が
  // "You Might Not Need an Effect" で推奨しない pattern。
  // 代わりに「prop 変化を render 中に検知して state を直接調整する」
  // pattern (https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes)
  // を使う。タイピング中の debounce navigate で URL が typed value に追従したとき
  // も同じ flow になるが、currentQ === value の no-op になるだけで focus は維持。
  const [prevCurrentQ, setPrevCurrentQ] = useState(currentQ);
  if (currentQ !== prevCurrentQ) {
    setPrevCurrentQ(currentQ);
    setValue(currentQ);
  }
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // unmount 時に保留中の debounce timer を解除する。これがないと 500ms 以内の
  // route 遷移で stale callback が `navigate()` (= updateSearchParams) を呼び、
  // unmount 後の component が router state を mutate しようとする leak になる。
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

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
        maxLength={MAX_QUERY_LENGTH}
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
