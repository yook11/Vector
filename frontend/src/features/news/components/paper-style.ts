import type { ArticleBrief } from "@/types/types.gen";

const CATEGORY_HUES: Record<string, string> = {
  AI: "#0E9E97",
  セキュリティ: "#C2562F",
  "市場・規制": "#B0852A",
  宇宙: "#5B6AB0",
  次世代ネットワーク: "#2F8F6B",
  次世代コンピューティング: "#7A5BA8",
  半導体: "#C04D6E",
  モビリティ: "#3F84C0",
  "ゲノム・バイオ": "#3F9E6B",
  次世代エネルギー: "#C08A2A",
  ロボティクス: "#6B7280",
  "素材・インフォマティクス": "#9A6BA8",
};

const SOURCE_HUES: Record<string, string> = {
  VentureBeat: "#E0392B",
  "Hacker News": "#FF6600",
  "Spaceflight Now": "#2F6FE0",
};

type MarkerPoint = readonly [number, number];

export function getCategoryHue(categoryName: string): string {
  return CATEGORY_HUES[categoryName] ?? "#0E9E97";
}

export function getCategoryMarkerRotation(categoryName: string): number {
  const total = Array.from(categoryName).reduce(
    (acc, char) => acc + char.charCodeAt(0),
    0,
  );
  return ((total % 7) - 3) * 0.4;
}

function makeMarkerRand(seed: number): () => number {
  let state = seed >>> 0 || 1;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function smoothMarkerPath(points: MarkerPoint[]): string {
  if (points.length === 0) return "";

  let d = "";
  for (let i = 0; i < points.length - 1; i += 1) {
    const [x0, y0] = points[i] as MarkerPoint;
    const [x1, y1] = points[i + 1] as MarkerPoint;
    const xc = (x0 + x1) / 2;
    const yc = (y0 + y1) / 2;
    d += `Q ${x0.toFixed(1)} ${y0.toFixed(1)} ${xc.toFixed(1)} ${yc.toFixed(1)} `;
  }

  const [lastX, lastY] = points[points.length - 1] as MarkerPoint;
  return `${d}L ${lastX.toFixed(1)} ${lastY.toFixed(1)} `;
}

function markerOpacity(opacity: number): (multiplier: number) => string {
  return (multiplier: number) => (opacity * multiplier).toFixed(3);
}

function markerDefs(
  seedA: number,
  seedB: number,
  width: number,
  height: number,
  centerLevel: number,
): string {
  const maskGradient = (value: number) => {
    const hex = Math.max(0, Math.min(255, Math.round(255 * value)))
      .toString(16)
      .padStart(2, "0");
    return `#${hex}${hex}${hex}`;
  };
  const mid = maskGradient(Math.min(1, centerLevel + 0.22));
  const center = maskGradient(centerLevel);

  return (
    "<filter id='rgh' x='-14%' y='-50%' width='128%' height='200%'>" +
    `<feTurbulence type='fractalNoise' baseFrequency='0.02 0.95' numOctaves='2' seed='${seedA}' result='n'/>` +
    "<feDisplacementMap in='SourceGraphic' in2='n' scale='6' xChannelSelector='R' yChannelSelector='G'/>" +
    "</filter>" +
    "<filter id='speck'>" +
    `<feTurbulence type='fractalNoise' baseFrequency='0.14 0.55' numOctaves='2' seed='${seedB}'/>` +
    "<feColorMatrix type='matrix' values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0.8 0 0 0 -0.36'/>" +
    "</filter>" +
    "<linearGradient id='vl' x1='0' y1='0' x2='0' y2='1'>" +
    "<stop offset='0' stop-color='#ffffff'/>" +
    `<stop offset='0.32' stop-color='${mid}'/>` +
    `<stop offset='0.5' stop-color='${center}'/>` +
    `<stop offset='0.68' stop-color='${mid}'/>` +
    "<stop offset='1' stop-color='#ffffff'/></linearGradient>" +
    "<mask id='wax' maskContentUnits='userSpaceOnUse'>" +
    `<rect x='0' y='0' width='${width}' height='${height}' fill='white'/>` +
    `<rect x='0' y='0' width='${width}' height='${height}' filter='url(#speck)'/>` +
    `<rect x='0' y='${(height * 0.16).toFixed(0)}' width='${width}' height='${(height * 0.68).toFixed(0)}' fill='url(#vl)'/>` +
    "</mask>"
  );
}

function markerDataUri(
  width: number,
  height: number,
  defs: string,
  body: string,
): string {
  const svg =
    `<svg xmlns='http://www.w3.org/2000/svg' width='${width}' height='${height}' viewBox='0 0 ${width} ${height}' preserveAspectRatio='none'>` +
    `<defs>${defs}</defs>${body}</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

function buildSwipeMarker(
  color: string,
  options: { centerLevel: number; opacity: number; seed: number },
): string {
  const rand = makeMarkerRand(options.seed);
  const width = 300;
  const height = 70;
  const segments = 7;
  const slope = (rand() * 2 - 1) * 5.5;
  const topY = 11 + (rand() * 2 - 1) * 2;
  const bottomY = 56 + (rand() * 2 - 1) * 2;

  const edge = (
    base: number,
    amplitude: number,
    phase: number,
    taperUp: boolean,
  ): MarkerPoint[] => {
    const points: MarkerPoint[] = [];
    for (let i = 0; i <= segments; i += 1) {
      const frac = i / segments;
      const x = 14 + frac * (width - 28);
      const taper = (1 - frac) ** 2.1 * 12 * (taperUp ? 1 : -1);
      const y =
        base +
        slope * frac +
        (rand() * 2 - 1) * amplitude +
        Math.sin(frac * Math.PI * 2.4 + phase) * 2.1 +
        taper;
      points.push([x, y]);
    }
    return points;
  };

  const top = edge(topY, 3, 0.4, true);
  const bottom = edge(bottomY, 3.4, 1.9, false);
  const bottomReversed = [...bottom].reverse();
  const [topLeftX, topLeftY] = top[0] as MarkerPoint;
  const [topRightX, topRightY] = top[top.length - 1] as MarkerPoint;
  const [bottomRightX, bottomRightY] = bottom[bottom.length - 1] as MarkerPoint;
  const [bottomLeftX, bottomLeftY] = bottom[0] as MarkerPoint;
  const midRight = (topRightY + bottomRightY) / 2;
  const midLeft = (topLeftY + bottomLeftY) / 2;

  const flickX = topRightX + 10 + rand() * 4;
  const flickY = topRightY - 12 - rand() * 5;
  let path = `M ${topLeftX.toFixed(1)} ${topLeftY.toFixed(1)} ${smoothMarkerPath(top)}`;
  path += `Q ${flickX.toFixed(1)} ${flickY.toFixed(1)} ${(topRightX + 8).toFixed(1)} ${(midRight - 2).toFixed(1)} `;
  path += `Q ${(bottomRightX + 7).toFixed(1)} ${(bottomRightY + 3).toFixed(1)} ${bottomRightX.toFixed(1)} ${bottomRightY.toFixed(1)} ${smoothMarkerPath(bottomReversed)}`;
  path += `Q ${(Math.min(topLeftX, bottomLeftX) - 14).toFixed(1)} ${midLeft.toFixed(1)} ${topLeftX.toFixed(1)} ${topLeftY.toFixed(1)} Z`;

  const core = edge((topY + bottomY) / 2, 2.4, 1.1, true);
  const [coreX, coreY] = core[0] as MarkerPoint;
  const corePath = `M ${coreX.toFixed(1)} ${coreY.toFixed(1)} ${smoothMarkerPath(core)}`;
  const opacity = markerOpacity(options.opacity);
  const jitter = () => 0.62 + rand() * 0.5;
  const stops =
    `<stop offset='0' stop-color='${color}' stop-opacity='${opacity(1.2)}'/>` +
    `<stop offset='0.18' stop-color='${color}' stop-opacity='${opacity(jitter())}'/>` +
    `<stop offset='0.4' stop-color='${color}' stop-opacity='${opacity(1)}'/>` +
    `<stop offset='0.6' stop-color='${color}' stop-opacity='${opacity(jitter())}'/>` +
    `<stop offset='0.82' stop-color='${color}' stop-opacity='${opacity(1)}'/>` +
    `<stop offset='1' stop-color='${color}' stop-opacity='${opacity(1.18)}'/>`;
  const defs =
    "<linearGradient id='g' x1='0' y1='0' x2='1' y2='0'>" +
    stops +
    "</linearGradient>" +
    markerDefs(
      Math.floor(rand() * 900),
      Math.floor(rand() * 900),
      width,
      height,
      options.centerLevel,
    );
  const body =
    "<g filter='url(#rgh)' mask='url(#wax)'>" +
    `<path d='${path}' fill='url(#g)'/>` +
    `<path d='${corePath}' fill='none' stroke='${color}' stroke-opacity='${opacity(0.3)}' stroke-width='13' stroke-linecap='round'/>` +
    "</g>";

  return markerDataUri(width, height, defs, body);
}

export function getCategoryMarkerImage(
  categoryName: string,
  seed: number,
): string {
  const baseSeed =
    seed + categoryName.length * 17 + (categoryName.codePointAt(0) ?? 1) * 7;
  return buildSwipeMarker(getCategoryHue(categoryName), {
    centerLevel: 0.42,
    opacity: 0.78,
    seed: baseSeed,
  });
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
