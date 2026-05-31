/**
 * Content Security Policy (CSP) ヘッダ構築の純関数群。
 *
 * 副作用 (Headers への set, NextResponse 生成) は呼び出し側 (proxy.ts) に残し、
 * ここでは「nonce 生成 / directive 配列構築 / 文字列 join」だけを担当する。
 *
 * NODE_ENV は引数 isDev として渡す (proxy.ts で判定)。
 * Vite が build 時に process.env.NODE_ENV を置換するため vi.stubEnv が
 * 効かない既知問題を、この境界設計で回避する。
 */

export function generateNonce(): string {
  return Buffer.from(crypto.randomUUID()).toString("base64");
}

export function buildCspDirectives(nonce: string, isDev: boolean): string[] {
  const nextStaticShellBootstrapHash =
    "'sha256-7mu4H06fwDCjmnxxr/xNHyuQC6pLTHr4M2E4jXw5WZs='";
  return [
    "default-src 'self'",
    // PPR の static shell script は self で許可し、inline script は nonce で縛る。
    `script-src 'self' 'nonce-${nonce}' ${nextStaticShellBootstrapHash}${isDev ? " 'unsafe-eval'" : ""}`,
    // Tailwind の動的クラス挿入のため unsafe-inline を許容 (将来的に nonce 化検討)
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'self'",
  ];
}

export function buildCspHeader(directives: string[]): string {
  return directives.join("; ");
}
