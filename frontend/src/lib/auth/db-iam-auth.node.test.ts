/**
 * RDS IAM 認証の pool config の不変条件。
 *
 * token 生成は SigV4 のローカル署名で完結するため、このテストは AWS に出ない
 * (dummy credential で署名だけ行う)。backend 側 `tests/test_db_iam_auth.py` と
 * 対称な構造で書く。
 */

import { Client } from "pg";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { runtimePoolConfigFromUrl } from "./db-iam-auth";

const RDS_URL =
  "postgresql://vector_auth@vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/vector?search_path=auth&sslmode=require";

function tokenParams(token: string): URLSearchParams {
  return new URLSearchParams(token.slice(token.indexOf("?") + 1));
}

beforeEach(() => {
  // 署名に要る credential と region。ローカル署名のみで実 AWS には出ない。
  vi.stubEnv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE");
  vi.stubEnv("AWS_SECRET_ACCESS_KEY", "secretexample");
  vi.stubEnv("AWS_REGION", "ap-northeast-1");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("runtimePoolConfigFromUrl — DB_IAM_AUTH が無効", () => {
  it.each([
    "false",
    "",
  ])("URL の password に任せる (Fly では経路が変わらない): DB_IAM_AUTH=%s", (flag) => {
    vi.stubEnv("DB_IAM_AUTH", flag);
    expect(runtimePoolConfigFromUrl(RDS_URL).password).toBeUndefined();
  });

  it("SSL 設定は素の poolConfigFromUrl と同じままにする", () => {
    vi.stubEnv("DB_IAM_AUTH", "false");
    expect(runtimePoolConfigFromUrl(RDS_URL).ssl).toEqual({
      rejectUnauthorized: true,
    });
  });
});

describe("runtimePoolConfigFromUrl — DB_IAM_AUTH が有効", () => {
  beforeEach(() => {
    vi.stubEnv("DB_IAM_AUTH", "true");
  });

  it("password は値ではなく生成器 (token は 15 分で失効する)", () => {
    expect(typeof runtimePoolConfigFromUrl(RDS_URL).password).toBe("function");
  });

  it("token は URL の user に対して署名される", async () => {
    const { password } = runtimePoolConfigFromUrl(RDS_URL);
    if (typeof password !== "function") {
      throw new Error("password must be a provider when IAM auth is enabled");
    }
    const token = await password();
    expect(tokenParams(token).get("DBUser")).toBe("vector_auth");
  });

  it("token は URL の endpoint に向く", async () => {
    const { password } = runtimePoolConfigFromUrl(RDS_URL);
    if (typeof password !== "function") {
      throw new Error("password must be a provider when IAM auth is enabled");
    }
    expect(await password()).toContain(
      "vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432",
    );
  });

  it("SSL 設定は保持する (認証と CA 検証は別の関心事)", () => {
    expect(runtimePoolConfigFromUrl(RDS_URL).ssl).toEqual({
      rejectUnauthorized: true,
    });
  });

  it("pg.Client に渡した後も password は生成器のまま残る (connectionString の再解析で password 関数が消える回帰の検出)", () => {
    const config = runtimePoolConfigFromUrl(RDS_URL);
    const client = new Client(config);
    expect(typeof client.password).toBe("function");
  });

  it("user の無い URL は拒否する (token は user 単位で署名する)", () => {
    expect(() =>
      runtimePoolConfigFromUrl(
        "postgresql://vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/vector?sslmode=require",
      ),
    ).toThrow(/user/);
  });
});
