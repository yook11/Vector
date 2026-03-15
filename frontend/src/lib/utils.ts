import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// --- XSS対策 Step 3: 出力エスケープ（URLスキームのホワイトリスト） ---
//
// 動的URLを href 属性に渡す場合、javascript: スキーム等による
// XSS攻撃を防ぐため、許可するプロトコルをホワイトリストで制限する。
//
// 攻撃例: <a href="javascript:alert(document.cookie)">Click</a>
//
// new URL() でパースする理由:
//   文字列の前方一致（startsWith("http")）では不十分。
//   "javascript:alert(1)//http://example.com" のようなケースを防げない。
//   URL パーサーに任せることで、ブラウザと同じ解釈でスキームを判定できる。
//
// 不正なURLの場合は null を返し、呼び出し元でリンク自体を非表示にする。
// null を使う理由: 空文字 "" は falsy だが「値がある」ことを暗示する。
// null は「安全なURLが存在しない」という意味を明示的に表現できる。
const SAFE_URL_PROTOCOLS = new Set(["http:", "https:"])

export function sanitizeUrl(url: string): string | null {
  try {
    const parsed = new URL(url)
    if (SAFE_URL_PROTOCOLS.has(parsed.protocol)) {
      return parsed.href
    }
    return null
  } catch {
    return null
  }
}
