import type { Mock } from "vitest";
import { vi } from "vitest";

// next/navigation の `useRouter()` が返す関数群を vi.fn 一式で再現する。
// vi.hoisted 内で `createRouterMock()` を呼び、`vi.mock("next/navigation", ...)`
// の factory から戻すことで、test 内から `mocks.router.push` 等を assertion
// できる。
//
// 使い方:
//   const mocks = vi.hoisted(() => ({
//     router: createRouterMock(),
//     searchParams: new URLSearchParams(),
//   }));
//   vi.mock("next/navigation", () => ({
//     useRouter: () => mocks.router,
//     usePathname: () => "/",
//     useSearchParams: () => mocks.searchParams,
//   }));
export type RouterMock = {
  push: Mock;
  replace: Mock;
  refresh: Mock;
  back: Mock;
  forward: Mock;
  prefetch: Mock;
};

export function createRouterMock(): RouterMock {
  return {
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  };
}
