import { describe, expect, it } from "vitest";
import { z } from "zod";
import {
  CLIENT_IP_HEADER,
  type ClientIpTrust,
  extractClientIp,
  parseClientIpTrust,
} from "./identifier";

// production は CLIENT_IP_TRUST で宣言された単一の出所のみ信頼し、それ以外は
// fail-closed で null (missing_ip)。development / test は trust を無視し現行の
// fallback (fly-client-ip → x-forwarded-for 先頭 → x-real-ip) を維持する。

describe("CLIENT_IP_HEADER", () => {
  it("proxy と downstream consumer が共有する内部ヘッダ名を固定する", () => {
    // 値そのものが本番 ALB/Fly の設定・下流 route の契約なので、意図しない変更を検知する。
    expect(CLIENT_IP_HEADER).toBe("x-vector-client-ip");
  });
});

describe("parseClientIpTrust", () => {
  it.each([
    "fly-client-ip",
    "alb-xff-last",
  ] as const)("有効値 %s をそのまま返す", (value) => {
    expect(parseClientIpTrust(value)).toBe(value);
  });

  it.each([
    undefined,
    null,
    "",
    "unknown-mode",
    "Fly-Client-IP",
    " fly-client-ip",
  ])("有効値以外 (%j) は null を返す", (value) => {
    expect(parseClientIpTrust(value as string | undefined | null)).toBeNull();
  });
});

describe("extractClientIp — production, trust=fly-client-ip", () => {
  const base = { trust: "fly-client-ip" as ClientIpTrust, isProduction: true };

  it("fly-client-ip の trim 値を採用する", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: "  203.0.113.10  ",
        forwardedFor: null,
        realIp: null,
      }),
    ).toBe("203.0.113.10");
  });

  it("x-forwarded-for / x-real-ip が来ても読まない", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: "203.0.113.10",
        forwardedFor: "9.9.9.9",
        realIp: "8.8.8.8",
      }),
    ).toBe("203.0.113.10");
  });

  it("fly-client-ip 欠如は他ヘッダがあっても fail-closed", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: null,
        forwardedFor: "9.9.9.9",
        realIp: "8.8.8.8",
      }),
    ).toBeNull();
  });

  it("fly-client-ip が空白のみでも fail-closed", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: "   ",
        forwardedFor: null,
        realIp: null,
      }),
    ).toBeNull();
  });
});

describe("extractClientIp — production, trust=alb-xff-last", () => {
  const base = { trust: "alb-xff-last" as ClientIpTrust, isProduction: true };

  it("x-forwarded-for の末尾要素の trim 値を採用する", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: null,
        forwardedFor: "1.2.3.4, 5.6.7.8",
        realIp: null,
      }),
    ).toBe("5.6.7.8");
  });

  it("末尾要素の前後空白を trim する", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: null,
        forwardedFor: "1.2.3.4,   5.6.7.8   ",
        realIp: null,
      }),
    ).toBe("5.6.7.8");
  });

  it("x-forwarded-for が単一値ならその値を採用する", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: null,
        forwardedFor: "5.6.7.8",
        realIp: null,
      }),
    ).toBe("5.6.7.8");
  });

  it("偽装された fly-client-ip があっても無視する (invariant: alb-xff-last で fly 偽装値が識別に使われない)", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: "203.0.113.66",
        forwardedFor: "1.2.3.4, 5.6.7.8",
        realIp: null,
      }),
    ).toBe("5.6.7.8");
  });

  it("x-forwarded-for 欠如は fail-closed (x-real-ip 等へ fallback しない)", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: null,
        forwardedFor: null,
        realIp: "9.9.9.9",
      }),
    ).toBeNull();
  });

  it("x-forwarded-for が空文字なら fail-closed", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: null,
        forwardedFor: "",
        realIp: null,
      }),
    ).toBeNull();
  });

  it("末尾要素が空白のみ (末尾カンマ) なら fail-closed", () => {
    expect(
      extractClientIp({
        ...base,
        flyClientIp: null,
        forwardedFor: "1.2.3.4, ",
        realIp: null,
      }),
    ).toBeNull();
  });
});

describe("extractClientIp — production, trust=null (未設定/不正値)", () => {
  it("全ヘッダが揃っていても null を返す (fail-closed)", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: "203.0.113.10",
        forwardedFor: "1.2.3.4, 5.6.7.8",
        realIp: "9.9.9.9",
        isProduction: true,
      }),
    ).toBeNull();
  });

  it("全ヘッダが欠如していても null を返す", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: null,
        forwardedFor: null,
        realIp: null,
        isProduction: true,
      }),
    ).toBeNull();
  });
});

describe("extractClientIp — 非 production は trust を無視し現行 fallback を維持する", () => {
  it.each([
    "fly-client-ip",
    "alb-xff-last",
    null,
  ] as const)("trust=%s でも fly-client-ip を最優先する", (trust) => {
    expect(
      extractClientIp({
        trust,
        flyClientIp: "203.0.113.10",
        forwardedFor: "1.2.3.4",
        realIp: null,
        isProduction: false,
      }),
    ).toBe("203.0.113.10");
  });

  it("fly-client-ip 欠如時は x-forwarded-for の先頭値 (末尾ではない) に fallback する", () => {
    expect(
      extractClientIp({
        trust: "alb-xff-last",
        flyClientIp: null,
        forwardedFor: "203.0.113.1, 198.51.100.1, 10.0.0.1",
        realIp: null,
        isProduction: false,
      }),
    ).toBe("203.0.113.1");
  });

  it("x-forwarded-for 先頭値の前後空白を trim する", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: null,
        forwardedFor: "  203.0.113.1  , 10.0.0.1",
        realIp: null,
        isProduction: false,
      }),
    ).toBe("203.0.113.1");
  });

  it("fly-client-ip / x-forwarded-for が両方欠如なら x-real-ip に fallback する", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: null,
        forwardedFor: null,
        realIp: "203.0.113.2",
        isProduction: false,
      }),
    ).toBe("203.0.113.2");
  });

  it("全ソースが欠如なら null を返す", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: null,
        forwardedFor: null,
        realIp: null,
        isProduction: false,
      }),
    ).toBeNull();
  });

  it("全ソースが空白のみなら null を返す", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: "   ",
        forwardedFor: "   ",
        realIp: "   ",
        isProduction: false,
      }),
    ).toBeNull();
  });
});

describe("extractClientIp — IP 構文検証", () => {
  // 識別単位の正規化 (specs/client-ip-trust-mode.md): IPv4 はそのまま、IPv4-mapped
  // IPv6 は埋め込み IPv4 に剥がしてから採用、その他の IPv6 は /64 network を
  // full-form (8 hextet 全展開、下位 4 hextet はゼロ埋め) に丸める。Better Auth の
  // /64 正規化と文字列一致させるため "::" 圧縮形にはしない。
  const VALID_IP_NORMALIZATIONS = [
    ["203.0.113.7", "203.0.113.7"],
    ["0.0.0.0", "0.0.0.0"],
    ["255.255.255.255", "255.255.255.255"],
    // /64 丸めの前に埋め込み IPv4 へ剥がす (丸めると全 mapped client が 1 バケツに畳まれるため)。
    ["::ffff:192.0.2.1", "192.0.2.1"],
    ["2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0000"], // 圧縮形
    ["::1", "0000:0000:0000:0000:0000:0000:0000:0000"], // loopback 圧縮形
    // full form。"::1" と同一 /64 (0000:0000:0000:0000::/64) なので同じ正規化結果になる。
    [
      "0000:0000:0000:0000:0000:0000:0000:0001",
      "0000:0000:0000:0000:0000:0000:0000:0000",
    ],
  ] as const;

  describe.each(
    VALID_IP_NORMALIZATIONS,
  )("妥当な IP %s は正規化後 %s として採用する", (ip, expected) => {
    it("production, trust=fly-client-ip", () => {
      expect(
        extractClientIp({
          trust: "fly-client-ip",
          flyClientIp: ip,
          forwardedFor: null,
          realIp: null,
          isProduction: true,
        }),
      ).toBe(expected);
    });

    it("production, trust=alb-xff-last (xff 末尾)", () => {
      expect(
        extractClientIp({
          trust: "alb-xff-last",
          flyClientIp: null,
          forwardedFor: `10.0.0.1, ${ip}`,
          realIp: null,
          isProduction: true,
        }),
      ).toBe(expected);
    });

    it("非 production fallback (fly-client-ip)", () => {
      expect(
        extractClientIp({
          trust: null,
          flyClientIp: ip,
          forwardedFor: null,
          realIp: null,
          isProduction: false,
        }),
      ).toBe(expected);
    });
  });

  // ALB の xff_client_port 有効時は ip:port 形式が付くため、明示的に境界値へ含める。
  const INVALID_VALUES = [
    "not-an-ip",
    "203.0.113.7:8080", // xff_client_port 有効時の ip:port 形式
    "203.0.113.999", // octet 範囲外
    "<script>",
  ] as const;

  describe.each(
    INVALID_VALUES,
  )("非IP値 %j は fail-closed (未解決扱い)", (invalid) => {
    it("production, trust=fly-client-ip", () => {
      expect(
        extractClientIp({
          trust: "fly-client-ip",
          flyClientIp: invalid,
          forwardedFor: null,
          realIp: null,
          isProduction: true,
        }),
      ).toBeNull();
    });

    it("production, trust=alb-xff-last (末尾が非IP)", () => {
      expect(
        extractClientIp({
          trust: "alb-xff-last",
          flyClientIp: null,
          forwardedFor: `1.2.3.4, ${invalid}`,
          realIp: null,
          isProduction: true,
        }),
      ).toBeNull();
    });

    it("非 production fallback: fly-client-ip 由来 (他ソースも無ければ null)", () => {
      expect(
        extractClientIp({
          trust: null,
          flyClientIp: invalid,
          forwardedFor: null,
          realIp: null,
          isProduction: false,
        }),
      ).toBeNull();
    });

    it("非 production fallback: x-forwarded-for 先頭由来 (他ソースも無ければ null)", () => {
      expect(
        extractClientIp({
          trust: null,
          flyClientIp: null,
          forwardedFor: `${invalid}, 5.6.7.8`,
          realIp: null,
          isProduction: false,
        }),
      ).toBeNull();
    });

    it("非 production fallback: x-real-ip 由来", () => {
      expect(
        extractClientIp({
          trust: null,
          flyClientIp: null,
          forwardedFor: null,
          realIp: invalid,
          isProduction: false,
        }),
      ).toBeNull();
    });
  });

  it("非 production fallback は不正な fly-client-ip をスキップし x-forwarded-for 先頭へ進む", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: "not-an-ip",
        forwardedFor: "203.0.113.1, 10.0.0.1",
        realIp: null,
        isProduction: false,
      }),
    ).toBe("203.0.113.1");
  });

  it("非 production fallback は不正な x-forwarded-for 先頭値をスキップし x-real-ip へ進む", () => {
    expect(
      extractClientIp({
        trust: null,
        flyClientIp: null,
        forwardedFor: "203.0.113.7:8080",
        realIp: "198.51.100.9",
        isProduction: false,
      }),
    ).toBe("198.51.100.9");
  });

  it("alb-xff-last は末尾が非IPだと途中の要素へ遡らない (単一ソースの厳格性、dev fallback とは別挙動)", () => {
    expect(
      extractClientIp({
        trust: "alb-xff-last",
        flyClientIp: null,
        forwardedFor: "203.0.113.1, not-an-ip",
        realIp: null,
        isProduction: true,
      }),
    ).toBeNull();
  });
});

describe("extractClientIp — IPv6 /64 正規化の不変条件", () => {
  it("同一 /64 内の異なるアドレス2つは同じ値に正規化される", () => {
    const a = extractClientIp({
      trust: "fly-client-ip",
      flyClientIp: "2001:db8:1:2::aaaa",
      forwardedFor: null,
      realIp: null,
      isProduction: true,
    });
    const b = extractClientIp({
      trust: "fly-client-ip",
      flyClientIp: "2001:db8:1:2:ffff:eeee:dddd:cccc",
      forwardedFor: null,
      realIp: null,
      isProduction: true,
    });
    expect(a).toBe("2001:0db8:0001:0002:0000:0000:0000:0000");
    expect(b).toBe(a);
  });

  it("異なる /64 は異なる値になる", () => {
    const a = extractClientIp({
      trust: "fly-client-ip",
      flyClientIp: "2001:db8:1:2::1",
      forwardedFor: null,
      realIp: null,
      isProduction: true,
    });
    const b = extractClientIp({
      trust: "fly-client-ip",
      flyClientIp: "2001:db8:1:3::1",
      forwardedFor: null,
      realIp: null,
      isProduction: true,
    });
    expect(a).not.toBe(b);
  });

  it.each([
    ["2001:DB8::1", "2001:0db8:0000:0000:0000:0000:0000:0000"], // 大文字
    ["2001:db8:0:0:0:0:0:1", "2001:0db8:0000:0000:0000:0000:0000:0000"], // 非圧縮 full form
    ["2001:db8::0:1", "2001:0db8:0000:0000:0000:0000:0000:0000"], // 部分圧縮
  ] as const)("表記ゆれ %s は同一 /64 なら同じ正規化結果 %s になる", (ip, expected) => {
    expect(
      extractClientIp({
        trust: "fly-client-ip",
        flyClientIp: ip,
        forwardedFor: null,
        realIp: null,
        isProduction: true,
      }),
    ).toBe(expected);
  });

  it("正規化後の値も IPv6 として構文妥当 (下流 Better Auth の isValidIP を通る形)", () => {
    const normalized = extractClientIp({
      trust: "fly-client-ip",
      flyClientIp: "2001:db8:aaaa:bbbb:1:2:3:4",
      forwardedFor: null,
      realIp: null,
      isProduction: true,
    });
    expect(normalized).toBe("2001:0db8:aaaa:bbbb:0000:0000:0000:0000");
    expect(z.ipv6().safeParse(normalized).success).toBe(true);
  });

  it("IPv4-mapped は異なる埋め込み IPv4 ごとに別の値になる (丸め前に剥がすため 1 バケツに畳まれない)", () => {
    const a = extractClientIp({
      trust: "fly-client-ip",
      flyClientIp: "::ffff:192.0.2.1",
      forwardedFor: null,
      realIp: null,
      isProduction: true,
    });
    const b = extractClientIp({
      trust: "fly-client-ip",
      flyClientIp: "::ffff:198.51.100.5",
      forwardedFor: null,
      realIp: null,
      isProduction: true,
    });
    expect(a).toBe("192.0.2.1");
    expect(b).toBe("198.51.100.5");
    expect(a).not.toBe(b);
  });
});
