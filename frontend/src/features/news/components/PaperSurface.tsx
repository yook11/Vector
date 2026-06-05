import type { ReactNode } from "react";
import { cn } from "@/lib/utils/cn";

interface PaperSurfaceProps {
  children: ReactNode;
  className?: string;
}

/** 紙面デザインの配色トークン (--vector-*) と基底背景・フォントを供給するラッパ。
 *  テクスチャ / overflow / sticky 制御は sticky 要素を壊さないため呼び出し側に委ねる。 */
export function PaperSurface({ children, className }: PaperSurfaceProps) {
  return (
    <div
      className={cn(
        "min-h-dvh bg-[var(--vector-paper)] text-[var(--vector-ink)] [--vector-accent:#0fa89c] [--vector-accent-ink:#08756f] [--vector-ink:#221c16] [--vector-ink-muted:#938a7c] [--vector-ink-soft:#5c544a] [--vector-line:#e4dccc] [--vector-paper:#f7f3ec] [--vector-rule:#d5ccbc] dark:[--vector-accent:#2dd4bf] dark:[--vector-accent-ink:#67e8d8] dark:[--vector-ink:#f3eee4] dark:[--vector-ink-muted:#8a8173] dark:[--vector-ink-soft:#b7ae9f] dark:[--vector-line:#332c23] dark:[--vector-paper:#14110b] dark:[--vector-rule:#40382d]",
        className,
      )}
      style={{ fontFamily: "var(--font-vector-sans)" }}
    >
      {children}
    </div>
  );
}
