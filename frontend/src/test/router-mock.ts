import type { Mock } from "vitest";
import { vi } from "vitest";

// next/navigation の `useRouter()` が返す関数群を vi.fn 一式で再現する。
// vi.hoisted 内では import を持ち込めない (top-level に巻き上げられるため
// vi.mock factory より先に評価される) ので、hoisted 内では inline 定義の
// router shape を使い、helper は `beforeEach` で再初期化する用途に使う。
//
// 使い方:
//   const mocks = vi.hoisted(() => ({
//     signInEmail: vi.fn(),
//     router: { push: vi.fn(), replace: vi.fn(), refresh: vi.fn(),
//               back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() },
//   }));
//   vi.mock("next/navigation", () => ({ useRouter: () => mocks.router }));
//
//   import { createRouterMock } from "@/test/router-mock";
//   beforeEach(() => {
//     vi.clearAllMocks();
//     Object.assign(mocks.router, createRouterMock());
//   });
//
// helper を使うことで、test 内から `mocks.router.push` 等を assertion でき、
// 各 test の vi.fn invocation history を完全に切り離せる。
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
