import { getCategoryKicker, kickerCssVars } from "./paper-style";

interface PaperKickerProps {
  slug: string;
  name: string;
}

/** カテゴリの二色スプリット記号 + 英字コード + カテゴリ名。カード・詳細ヘッダで共用。 */
export function PaperKicker({ slug, name }: PaperKickerProps) {
  const kicker = getCategoryKicker(slug);

  return (
    <span className="inline-flex min-w-0 items-center gap-2.5">
      <span
        aria-hidden="true"
        className="size-[11px] shrink-0 bg-[linear-gradient(135deg,var(--kc-hue)_0_50%,var(--vector-ink)_50%_100%)] dark:bg-[linear-gradient(135deg,var(--kc-hue-dark)_0_50%,var(--vector-ink)_50%_100%)]"
        style={kickerCssVars(kicker)}
      />
      <span
        className="shrink-0 text-[12.5px] font-semibold tracking-[0.22em] text-[var(--vector-ink)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        {kicker.code}
      </span>
      <span
        className="truncate text-[10px] font-medium tracking-[0.08em] text-[var(--vector-ink-muted)]"
        style={{ fontFamily: "var(--font-vector-maru)" }}
        title={name}
      >
        {name}
      </span>
    </span>
  );
}
