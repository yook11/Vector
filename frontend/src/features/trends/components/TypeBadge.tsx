import type { MentionType } from "@/types";
import { MENTION_TYPE_META } from "../display";

interface TypeBadgeProps {
  type: MentionType;
}

/** 固有名の種別を小四角+ラベルで示す純コンポーネント。 */
export function TypeBadge({ type }: TypeBadgeProps) {
  const meta = MENTION_TYPE_META[type];
  return (
    <span
      className="inline-flex items-center gap-1 shrink-0"
      style={{ fontFamily: "var(--font-vector-maru)" }}
    >
      <span
        aria-hidden="true"
        className="inline-block size-[7px] rounded-[1px] shrink-0"
        style={{ backgroundColor: meta.color }}
      />
      <span className="text-[10.5px] tracking-[0.04em] text-[var(--vector-ink-muted)]">
        {meta.label}
      </span>
    </span>
  );
}
