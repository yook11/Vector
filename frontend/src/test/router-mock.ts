import type { Mock } from "vitest";
import { vi } from "vitest";

// vi.hoisted 内では import できないため、各 test の beforeEach で router mock を再生成する。
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
