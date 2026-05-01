import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./src/test/msw/server";

// msw lifecycle: 未 handler の request は `bypass` で透過させ、既存の
// vi.mock + 相対 path mock を使う test に影響を与えない。各 test 内で
// `server.use(http.<method>(url, ...))` した endpoint のみ intercept される。
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

// vitest.config の `globals: false` のため RTL の auto cleanup が register
// されない。明示的に afterEach で cleanup() を呼ばないと前 test の DOM が
// 残留し `getByLabelText` で multiple match が起きる。
afterEach(() => {
  cleanup();
});
