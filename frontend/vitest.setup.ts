import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest.config の `globals: false` のため RTL の auto cleanup が register
// されない。明示的に afterEach で cleanup() を呼ばないと前 test の DOM が
// 残留し `getByLabelText` で multiple match が起きる。
afterEach(() => {
  cleanup();
});
