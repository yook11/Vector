/**
 * 表示窓の許容値・default・label↔windowHours 変換を集約した server-safe policy。
 *
 * "use client" を付けないことで Server Component (page.tsx) と Client Component
 * (SourceHealthWindowSelect) の両方から import できる。URL には人間可読な label
 * (24h/48h/72h/7d) を載せ、API の windowHours (24/48/72/168) への変換はここに閉じる。
 */

import type { WindowHours } from "@/types/types.gen";

export const WINDOW_OPTIONS = ["24h", "48h", "72h", "7d"] as const;

export type WindowOption = (typeof WINDOW_OPTIONS)[number];

export const DEFAULT_WINDOW: WindowOption = "24h";

// label → API windowHours。生成型 WindowHours (= 24 | 48 | 72 | 168) と噛み合わせる。
const WINDOW_HOURS: Record<WindowOption, WindowHours> = {
  "24h": 24,
  "48h": 48,
  "72h": 72,
  "7d": 168,
};

const WINDOW_OPTION_SET = new Set<string>(WINDOW_OPTIONS);

export function isWindowOption(value: string): value is WindowOption {
  return WINDOW_OPTION_SET.has(value);
}

export function windowToHours(window: WindowOption): WindowHours {
  return WINDOW_HOURS[window];
}

// response の windowHours は number 型 (backend schema は int) なので number 受け。
const HOURS_TO_WINDOW: Record<number, WindowOption> = {
  24: "24h",
  48: "48h",
  72: "72h",
  168: "7d",
};

// API の windowHours を表示用 label に戻す (7d を "168h" と表示しないため)。
// 想定外の値は default に落とす。
export function hoursToWindow(hours: number): WindowOption {
  return HOURS_TO_WINDOW[hours] ?? DEFAULT_WINDOW;
}

/**
 * searchParams の生値 (string | string[] | undefined) を WindowOption に正規化する。
 * 不正値・配列・未指定はすべて DEFAULT_WINDOW ("24h") に落とす。
 */
export function resolveWindow(
  raw: string | string[] | undefined,
): WindowOption {
  if (typeof raw === "string" && isWindowOption(raw)) {
    return raw;
  }
  return DEFAULT_WINDOW;
}
