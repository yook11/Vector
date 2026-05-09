import type { ReactNode } from "react";

type Tone = "baseline" | "option";

interface OptionFrameProps {
  tone?: Tone;
  badge: string;
  title: string;
  intent: string;
  pros: readonly string[];
  cons: readonly string[];
  children: ReactNode;
}

export function OptionFrame({
  tone = "option",
  badge,
  title,
  intent,
  pros,
  cons,
  children,
}: OptionFrameProps) {
  const isBaseline = tone === "baseline";
  return (
    <section className="border-t border-border/60">
      <div className="mx-auto max-w-4xl px-4 py-12 sm:py-14">
        <header className="mb-2 flex items-center gap-3">
          <span
            className={`inline-flex items-center justify-center rounded-md px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.18em] ${
              isBaseline
                ? "bg-muted text-muted-foreground"
                : "bg-primary text-primary-foreground"
            }`}
          >
            {badge}
          </span>
          <h2 className="text-xl font-medium tracking-tight text-foreground sm:text-2xl">
            {title}
          </h2>
        </header>
        <p className="mb-6 max-w-2xl text-sm leading-relaxed text-muted-foreground">
          {intent}
        </p>
        <div className="grid max-w-3xl gap-4 sm:grid-cols-2">
          <ProsCons label="Pros" items={pros} accent="positive" />
          <ProsCons label="Cons" items={cons} accent="negative" />
        </div>
      </div>

      <div className="border-t border-dashed border-border/60 bg-muted/40">
        {children}
      </div>
    </section>
  );
}

function ProsCons({
  label,
  items,
  accent,
}: {
  label: string;
  items: readonly string[];
  accent: "positive" | "negative";
}) {
  const dotColor =
    accent === "positive" ? "bg-emerald-500/80" : "bg-amber-500/80";
  return (
    <div>
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </p>
      <ul className="space-y-1.5">
        {items.map((item) => (
          <li
            key={item}
            className="flex items-start gap-2 text-sm leading-relaxed text-foreground/90"
          >
            <span
              aria-hidden="true"
              className={`mt-2 size-1 shrink-0 rounded-full ${dotColor}`}
            />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
