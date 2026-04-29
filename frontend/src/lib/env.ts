/**
 * 必須環境変数の取得 utility。
 *
 * `??` フォールバックや空文字許容は持たせない方針 — 未設定時は呼び出し側で
 * 即時 throw し fail-fast にする (build / 起動時に発覚させる)。`hint` は
 * generate コマンド等の補助情報を error message に添える用。
 */

export function requireEnv(name: string, hint?: string): string {
  const value = process.env[name];
  if (!value) {
    const suffix = hint ? `; ${hint}` : "";
    throw new Error(`${name} is required${suffix}`);
  }
  return value;
}
