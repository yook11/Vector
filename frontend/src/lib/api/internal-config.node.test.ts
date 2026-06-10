/**
 * `buildBffRequestHeaders` の単体 test (node project)。
 *
 * 実際に jose で JWT を署名するため、cross-realm Uint8Array 問題を避けて本番と
 * 同じ Node 実行環境で回す (`.node.test.ts`)。署名鍵は backend と同じ secret を
 * 使い、`jwtVerify` で claim を検証する。
 */

import { jwtVerify } from "jose";
import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

// internal-config.ts は module top-level で env を要求するため、import 前に
// valid な値を埋める (vi.hoisted で import より先に評価される)。
vi.hoisted(() => {
  process.env.INTERNAL_API_URL = "http://backend:8000/api/v1";
  process.env.BFF_JWT_SIGNING_SECRET = "0".repeat(64);
});

import { buildBffRequestHeaders } from "./internal-config";

describe("buildBffRequestHeaders — user-less BFF 経由証明 JWT", () => {
  const SECRET = new TextEncoder().encode("0".repeat(64));

  it("iss/aud/exp を持ち sub/role を含まない (backend secret で検証可能)", async () => {
    const headers = await buildBffRequestHeaders();
    expect(headers.Authorization).toMatch(/^Bearer /);
    const token = (headers.Authorization ?? "").slice("Bearer ".length);

    const { payload } = await jwtVerify(token, SECRET, {
      issuer: "vector-bff",
      audience: "vector-backend",
    });
    expect(typeof payload.exp).toBe("number");
    expect(typeof payload.iat).toBe("number");
    // user 非依存の BFF 経由証明なので login claim を持たない。
    expect(payload.sub).toBeUndefined();
    expect(payload.role).toBeUndefined();
  });
});
