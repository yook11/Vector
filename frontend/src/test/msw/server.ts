import { setupServer } from "msw/node";

// グローバルな handler を持たず、各 test ファイル内で `server.use(...)` で
// 都度定義する原則。features 横断の handler 集約を作らないことで、
// frontend/CLAUDE.md の「features 横断 module を mock してはならない」と
// 構造的に整合する。
export const server = setupServer();
