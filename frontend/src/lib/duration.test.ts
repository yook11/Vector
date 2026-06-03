import { describe, expect, it } from "vitest";
import { formatAgeSeconds } from "./duration";

describe("formatAgeSeconds", () => {
  it("null は欠損として '-' を返す", () => {
    expect(formatAgeSeconds(null)).toBe("-");
  });

  it("時間と分を 'Nh Nm' で表示する", () => {
    expect(formatAgeSeconds(4320)).toBe("1h 12m"); // 72 min
  });

  it("分が 0 の時間は 'Nh' のみにする", () => {
    expect(formatAgeSeconds(3600)).toBe("1h");
  });

  it("1時間未満は 'Nm' で表示する", () => {
    expect(formatAgeSeconds(720)).toBe("12m"); // 12 min
  });

  it("1分未満は '0m' に切り捨てる", () => {
    expect(formatAgeSeconds(30)).toBe("0m");
  });

  it("ちょうど 0 秒も '0m'", () => {
    expect(formatAgeSeconds(0)).toBe("0m");
  });

  it("24時間を超えても日単位にせず時間で積む", () => {
    expect(formatAgeSeconds(90000)).toBe("25h"); // 1500 min = 25h
  });
});
