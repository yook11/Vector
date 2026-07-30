/**
 * ElastiCache IAM 認証の接続 options の不変条件。
 *
 * token 生成は SigV4 のローカル署名で完結するため、このテストは AWS/Redis に出ない
 * (dummy credential で署名だけ行う)。`db-iam-auth.node.test.ts` と対称な構造で書く。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  type RedisIamConnection,
  redisIamConnectionOptions,
} from "./redis-iam-auth";

const REDIS_URL =
  "rediss://vector_rl@vector-rate-limit.abc123.ng.0001.apne1.cache.amazonaws.com:6379/0";

function tokenParams(token: string): URLSearchParams {
  return new URLSearchParams(token.slice(token.indexOf("?") + 1));
}

// credentialsProvider は union 型 (async / streaming) なので、テスト用に
// async 側であることを確認しつつ credentials() を呼ぶ。
async function credentials(
  connection: RedisIamConnection,
): Promise<{ username?: string; password?: string }> {
  if (connection.credentialsProvider?.type !== "async-credentials-provider") {
    throw new Error(
      "credentialsProvider must be async when IAM auth is enabled",
    );
  }
  return connection.credentialsProvider.credentials();
}

beforeEach(() => {
  // 署名に要る credential と region。ローカル署名のみで実 AWS には出ない。
  vi.stubEnv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE");
  vi.stubEnv("AWS_SECRET_ACCESS_KEY", "secretexample");
  vi.stubEnv("AWS_REGION", "ap-northeast-1");
  vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "vector-rate-limit");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("redisIamConnectionOptions — REDIS_IAM_AUTH が無効", () => {
  it.each([
    "false",
    "",
  ])("URL をそのまま返し credentialsProvider を付けない (dev / Fly 不変): REDIS_IAM_AUTH=%s", (flag) => {
    vi.stubEnv("REDIS_IAM_AUTH", flag);
    expect(redisIamConnectionOptions(REDIS_URL)).toEqual({ url: REDIS_URL });
  });

  it("誤設定 (cache 名・region 欠落) でも off なら無視されて URL をそのまま返す", () => {
    vi.stubEnv("REDIS_IAM_AUTH", "false");
    vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "");
    vi.stubEnv("AWS_REGION", "");
    expect(redisIamConnectionOptions(REDIS_URL)).toEqual({ url: REDIS_URL });
  });
});

describe("redisIamConnectionOptions — REDIS_IAM_AUTH が有効", () => {
  beforeEach(() => {
    vi.stubEnv("REDIS_IAM_AUTH", "true");
  });

  it("userinfo を除いた URL を返す (scheme / host / port / path は保つ)", () => {
    expect(redisIamConnectionOptions(REDIS_URL).url).toBe(
      "rediss://vector-rate-limit.abc123.ng.0001.apne1.cache.amazonaws.com:6379/0",
    );
  });

  it("credentialsProvider は async-credentials-provider 型で返す", () => {
    const { credentialsProvider } = redisIamConnectionOptions(REDIS_URL);
    expect(credentialsProvider?.type).toBe("async-credentials-provider");
  });

  it("credentials() の username は URL の user と一致する", async () => {
    const { username } = await credentials(
      redisIamConnectionOptions(REDIS_URL),
    );
    expect(username).toBe("vector_rl");
  });

  it("credentials() の password は空でない token を返す (空だと AUTH 節が省略され default user 行きになる)", async () => {
    const { password } = await credentials(
      redisIamConnectionOptions(REDIS_URL),
    );
    expect(password).toBeTruthy();
    expect(password?.length).toBeGreaterThan(0);
  });

  it("token は `<cacheName>/?` で始まり scheme を含まない", async () => {
    const { password } = await credentials(
      redisIamConnectionOptions(REDIS_URL),
    );
    expect(password?.startsWith("vector-rate-limit/?")).toBe(true);
    expect(password).not.toContain("https://");
  });

  it("token の query に Action=connect / User=<user> / X-Amz-Signature / X-Amz-Expires=900 を含む", async () => {
    const { password } = await credentials(
      redisIamConnectionOptions(REDIS_URL),
    );
    const params = tokenParams(password ?? "");
    expect(params.get("Action")).toBe("connect");
    expect(params.get("User")).toBe("vector_rl");
    expect(params.get("X-Amz-Signature")).toBeTruthy();
    expect(params.get("X-Amz-Expires")).toBe("900");
  });

  it("username の無い URL は拒否する (token は user 単位で署名する)", () => {
    expect(() =>
      redisIamConnectionOptions(
        "rediss://vector-rate-limit.abc123.ng.0001.apne1.cache.amazonaws.com:6379/0",
      ),
    ).toThrow(/user/);
  });

  it("REDIS_IAM_CACHE_NAME_RL 欠落は拒否する (token は cache 名に対して署名する)", () => {
    vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "");
    expect(() => redisIamConnectionOptions(REDIS_URL)).toThrow();
  });

  it("AWS_REGION 欠落は拒否する", () => {
    vi.stubEnv("AWS_REGION", "");
    expect(() => redisIamConnectionOptions(REDIS_URL)).toThrow();
  });

  it("URL としてパースできない誤設定は throw し、message に元の URL 文字列 (password 込み) を含めない", () => {
    const malformedUrlWithPassword =
      "redis://vector_rl:SECRET_PASSWORD@[unterminated-bracket-host";
    let thrown: unknown;
    try {
      redisIamConnectionOptions(malformedUrlWithPassword);
    } catch (err) {
      thrown = err;
    }
    expect(thrown).toBeInstanceOf(Error);
    const message = (thrown as Error).message;
    expect(message).not.toContain(malformedUrlWithPassword);
    expect(message).not.toContain("SECRET_PASSWORD");
    expect(message).toBe("the rate limit Redis URL is not a parsable URL");
  });
});
