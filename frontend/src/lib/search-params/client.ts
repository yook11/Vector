/**
 * URL search params の Client 側フック群。
 *
 * 既存の `params.delete("page")` 系の重複を排除し、URL 更新ロジックを
 * 1 箇所に集約する。usePathname を使うことで現在のパスに対して動作する
 * (旧実装の hardcoded "/" を排除)。
 */

"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

export type ParamUpdates = Record<string, string | undefined>;

function applyUpdates(
  base: URLSearchParams,
  updates: ParamUpdates,
): URLSearchParams {
  const next = new URLSearchParams(base.toString());
  for (const [key, value] of Object.entries(updates)) {
    if (value === undefined || value === "") {
      next.delete(key);
    } else {
      next.set(key, value);
    }
  }
  return next;
}

/**
 * 現在の URL search params に updates を適用し、現在のパスを保ったまま
 * `router.push` するフック。`undefined` / 空文字は delete として扱う。
 */
export function useUpdateSearchParams() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  return useCallback(
    (updates: ParamUpdates) => {
      const next = applyUpdates(
        new URLSearchParams(searchParams?.toString() ?? ""),
        updates,
      );
      const qs = next.toString();
      router.push(qs ? `${pathname}?${qs}` : pathname);
    },
    [router, pathname, searchParams],
  );
}

/**
 * `useUpdateSearchParams` の navigate を行わない版。`<Link href={...}>` を
 * 構築する場面 (CategorySidebar 等) で利用。
 */
export function useBuildSearchParamsHref() {
  const pathname = usePathname();
  const searchParams = useSearchParams();

  return useCallback(
    (updates: ParamUpdates): string => {
      const next = applyUpdates(
        new URLSearchParams(searchParams?.toString() ?? ""),
        updates,
      );
      const qs = next.toString();
      return qs ? `${pathname}?${qs}` : pathname;
    },
    [pathname, searchParams],
  );
}
