import type { CSSProperties } from "react";
import type { ArticleBrief } from "@/types/types.gen";

// カテゴリは slug をキーにする。表示名は migration で変わりうるが slug は不変。
const CATEGORY_META: Record<string, { code: string; hue: string }> = {
  ai: { code: "A.I.", hue: "#0E9E97" },
  bio: { code: "BIO", hue: "#6E8B3D" },
  computing: { code: "COMPUTE", hue: "#7A5BA8" },
  energy: { code: "ENERGY", hue: "#B5752E" },
  materials: { code: "MATERIALS", hue: "#6E5A8C" },
  mobility: { code: "MOBILITY", hue: "#3F84C0" },
  network: { code: "NETWORK", hue: "#2F8F6B" },
  other: { code: "MARKET", hue: "#B0852A" },
  robotics: { code: "ROBOTICS", hue: "#8A6A4F" },
  security: { code: "SECURITY", hue: "#C2562F" },
  semiconductor: { code: "SEMICON", hue: "#C04D6E" },
  space: { code: "SPACE", hue: "#5B6AB0" },
};

const CATEGORY_FALLBACK = { code: "NEWS", hue: "#0E9E97" } as const;

const SOURCE_HUES: Record<string, string> = {
  VentureBeat: "#E0392B",
  "Hacker News": "#FF6600",
  "Spaceflight Now": "#2F6FE0",
};

// 白方向に混色。SSR では theme を JS 分岐できないため dark hue を事前計算して渡す。
function lightenHex(hex: string, amount: number): string {
  const m = hex.replace("#", "");
  const mix = (i: number) => {
    const c = Number.parseInt(m.slice(i, i + 2), 16);
    return Math.round(c + (255 - c) * amount)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${mix(0)}${mix(2)}${mix(4)}`;
}

export interface CategoryKicker {
  code: string;
  hue: string;
  hueDark: string;
}

export function getCategoryKicker(slug: string): CategoryKicker {
  const meta = CATEGORY_META[slug] ?? CATEGORY_FALLBACK;
  return {
    code: meta.code,
    hue: meta.hue,
    hueDark: lightenHex(meta.hue, 0.28),
  };
}

/** kicker の hue を二色記号・短罫が参照する CSS 変数へ。SSR では dark を JS 分岐
 *  できないため light/dark 両値を流し、消費側が dark: variant で切替える。 */
export function kickerCssVars(kicker: CategoryKicker): CSSProperties {
  return {
    "--kc-hue": kicker.hue,
    "--kc-hue-dark": kicker.hueDark,
  } as CSSProperties;
}

export function getSourceBadge(sourceName: string): {
  color: string;
  short: string;
} {
  const color = SOURCE_HUES[sourceName] ?? "#0FA89C";
  if (sourceName === "VentureBeat") return { color, short: "VB" };
  if (sourceName === "Hacker News") return { color, short: "Y" };
  if (sourceName === "Spaceflight Now") return { color, short: "SN" };

  const short =
    sourceName
      .split(/\s+/)
      .filter(Boolean)
      .map((word) => word[0])
      .join("")
      .slice(0, 2)
      .toUpperCase() || "·";
  return { color, short };
}

export function getArticleSourceLabel(article: ArticleBrief): string {
  return article.source.attributionLabel ?? article.source.name;
}

export function formatPaperDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "日付不明";
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "Asia/Tokyo",
  }).format(new Date(dateStr));
}

export function formatPaperTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  return new Intl.DateTimeFormat("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Tokyo",
  }).format(new Date(dateStr));
}

export function formatPaperMastheadDate(date: Date): string {
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
    timeZone: "Asia/Tokyo",
  }).format(date);
}

export function getLatestArticleDate(items: ArticleBrief[]): Date {
  const timestamps = items
    .map((item) =>
      item.publishedAt ? new Date(item.publishedAt).getTime() : Number.NaN,
    )
    .filter(Number.isFinite);

  if (timestamps.length === 0) return new Date();
  return new Date(Math.max(...timestamps));
}
