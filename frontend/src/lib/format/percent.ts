/**
 * 伸び率を表示文字列に整形する。負符号は U+2212(MINUS SIGN)を使う。
 * 例: +42%, −7%
 */
export function formatGrowthRate(rate: number): string {
  const sign = rate >= 0 ? "+" : "−";
  return `${sign}${Math.round(Math.abs(rate) * 100)}%`;
}
