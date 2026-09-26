import { readFileSync } from "node:fs";
import { createSecureContext } from "node:tls";
import { inspect } from "node:util";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { poolConfigFromUrl } from "./pool-ssl";

// TLS を使わない経路 (dev / CI / build) で証明書ファイルを読まないことを観測する。
vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  return { ...actual, readFileSync: vi.fn(actual.readFileSync) };
});

beforeEach(() => {
  vi.mocked(readFileSync).mockClear();
});

describe("poolConfigFromUrl", () => {
  it("sslmode=require は RDS の root だけを ca に渡して検証を有効化し、sslmode を connectionString から除く", () => {
    const { connectionString, ssl } = poolConfigFromUrl(
      "postgresql://u:p@db.example.ap-northeast-1.rds.amazonaws.com/vector?sslmode=require",
    );

    expect(ssl).toEqual({
      ca: readFileSync("rds-ca-ap-northeast-1.pem", "utf8"),
      rejectUnauthorized: true,
    });
    expect(connectionString).not.toContain("sslmode");
  });

  it("ca は RDS の regional root 3 本で、TLS の信頼集合として読み込める", () => {
    const { ssl } = poolConfigFromUrl(
      "postgresql://u:p@db.example.ap-northeast-1.rds.amazonaws.com/vector?sslmode=require",
    );
    if (typeof ssl !== "object" || typeof ssl.ca !== "string") {
      throw new Error(
        "sslmode=require では ca に PEM 文字列を渡す必要があります。",
      );
    }

    expect(ssl.ca.match(/-----BEGIN CERTIFICATE-----/g)).toHaveLength(3);
    expect(() => createSecureContext({ ca: ssl.ca })).not.toThrow();
  });

  it("sslmode なし (dev / docker) は SSL を無効化し、証明書ファイルを読まない", () => {
    const { ssl } = poolConfigFromUrl("postgresql://u:p@db:5432/vector");

    expect(ssl).toBe(false);
    expect(readFileSync).not.toHaveBeenCalled();
  });

  it("sslmode=disable は明示的に SSL を無効化し、証明書ファイルを読まない", () => {
    const { ssl } = poolConfigFromUrl(
      "postgresql://u:p@db:5432/vector?sslmode=disable",
    );

    expect(ssl).toBe(false);
    expect(readFileSync).not.toHaveBeenCalled();
  });

  it("search_path など他の query param は保持する", () => {
    const { connectionString } = poolConfigFromUrl(
      "postgresql://u:p@db:5432/vector?search_path=auth",
    );

    expect(connectionString).toContain("search_path=auth");
  });

  it("malformed URL の例外表示やown propertyにcredentialを含めない", () => {
    const passwordSentinel = "POOL-SSL-PASSWORD-MUST-NOT-LEAK";
    const rawUrl = `postgresql://vector_auth:${passwordSentinel}@[invalid-host:5432/vector`;
    let thrown: unknown;

    try {
      poolConfigFromUrl(rawUrl);
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(Error);
    if (typeof thrown !== "object" || thrown === null) {
      throw new Error("malformed URL は Error をthrowする必要があります。");
    }
    const ownProperties = Object.fromEntries(
      Object.getOwnPropertyNames(thrown).map((name) => [
        name,
        Reflect.get(thrown, name),
      ]),
    );
    const renderings = {
      string: String(thrown),
      json: JSON.stringify(thrown) ?? "",
      inspect: inspect(thrown),
      ownProperties: inspect(ownProperties),
    };

    for (const [name, rendered] of Object.entries(renderings)) {
      expect(rendered, `${name} must redact the raw URL`).not.toContain(rawUrl);
      expect(rendered, `${name} must redact the password`).not.toContain(
        passwordSentinel,
      );
    }
  });
});
