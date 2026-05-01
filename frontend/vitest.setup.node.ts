import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./src/test/msw/server";

// rsc (node) project の setup。client setup と異なり @testing-library/* は
// 一切 import しない (node 環境で DOM を持たない / page-model は JSX 非依存)。
// msw lifecycle のみ提供して page-model から内部的に走る fetch を制御可能に
// しておく (現状の page-model は fetch を直接叩かないが、将来の補助 fetch に
// 備えて両 project で同じ network 制御 API を提供する)。
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
