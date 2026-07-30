/**
 * `assertAllowedInternalApiUrl` の単体 test。
 *
 * 検証対象は 2 段:
 * - global allowlist (全環境共通): localhost / 127.0.0.1 / backend / 実行基盤の
 *   内部 namespace (*.flycast / *.vector.internal) 以外を拒否
 * - production narrowing (NODE_ENV="production"): 内部 namespace 以外を拒否
 *
 * backend 側 `_validate_internal_frontend_base_url` +
 * `_enforce_internal_namespace_in_production` (backend/tests/test_config.py) と
 * 対称な構造で書く。
 *
 * `nodeEnv` を引数化したことで env を tampering せず純粋関数として検証できる
 * (env stub は module top-level の `_loadInternalApiUrl()` が import 時に
 * throw しないようにするためだけの予防策)。
 */

import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

// internal-config.ts の module top-level で `requireEnv("INTERNAL_API_URL")` +
// `assertAllowedInternalApiUrl(url)` が走るため、import 前に valid な env を
// 埋めておく (vi.hoisted で import より先に評価される)。NODE_ENV はテスト時
// "test" のため production narrowing は効かない (これは想定挙動)。
vi.hoisted(() => {
  process.env.INTERNAL_API_URL = "http://backend:8000/api/v1";
  process.env.BFF_JWT_SIGNING_SECRET = "0".repeat(64);
});

import { assertAllowedInternalApiUrl } from "./internal-config";

const DEV_HOST_URLS = [
  "http://backend:8000/api/v1",
  "http://localhost:8000/api/v1",
  "http://127.0.0.1:8000/api/v1",
] as const;

// 実行基盤の内部 namespace。Fly と AWS の両方を同じ allowlist が受理する。
// backend 側 (test_config.py の _INTERNAL_NAMESPACE_URLS) が同じ 2 つを固定している。
const INTERNAL_NAMESPACE_URLS = [
  "http://your-vector-core-app.flycast:8000/api/v1",
  "http://api.vector.internal:8000/api/v1",
] as const;

// allowlist の境界。suffix の前の dot と「末尾であること」の 2 つが効いている。
const REJECTED_URLS = [
  "http://evil.example.com/api/v1",
  "http://xflycast:8000/api/v1",
  "http://your-vector-core-app.flycast.attacker.com:8000/api/v1",
  "http://evilvector.internal:8000/api/v1",
  "http://api.vector.internal.attacker.com:8000/api/v1",
  // .internal だが自分の namespace ではない (許すのは TLD 全体ではない)。
  "http://api.attacker.internal:8000/api/v1",
] as const;

describe("assertAllowedInternalApiUrl — global allowlist (全環境共通)", () => {
  it.each(DEV_HOST_URLS)("development では dev host を許可: %s", (url) => {
    expect(() => assertAllowedInternalApiUrl(url, "development")).not.toThrow();
  });

  it.each(
    INTERNAL_NAMESPACE_URLS,
  )("development で内部 namespace を許可: %s", (url) => {
    expect(() => assertAllowedInternalApiUrl(url, "development")).not.toThrow();
  });

  it.each(
    REJECTED_URLS,
  )("allowlist 外 host を拒否 (BFF JWT 持ち出し経路を構造遮断): %s", (url) => {
    expect(() => assertAllowedInternalApiUrl(url, "development")).toThrow(
      /not an allowed internal destination/,
    );
  });

  it.each([
    "gopher://backend/api/v1",
    "file:///etc/passwd",
  ])("http / https 以外の scheme を拒否: %s", (bad) => {
    expect(() => assertAllowedInternalApiUrl(bad, "development")).toThrow(
      /http or https/,
    );
  });

  it("URL として parse 不能な値を拒否", () => {
    expect(() =>
      assertAllowedInternalApiUrl("not-a-url", "development"),
    ).toThrow(/not a valid URL/);
  });
});

describe("assertAllowedInternalApiUrl — production narrowing", () => {
  it.each(
    INTERNAL_NAMESPACE_URLS,
  )("production で内部 namespace を許可: %s", (url) => {
    expect(() => assertAllowedInternalApiUrl(url, "production")).not.toThrow();
  });

  it.each(
    DEV_HOST_URLS,
  )("production では dev host を拒否 (内部 namespace のみ許可): %s", (url) => {
    expect(() => assertAllowedInternalApiUrl(url, "production")).toThrow(
      /production/,
    );
  });
});
