import { cn } from "@/lib/utils";

function getColor(score: number): string {
  if (score >= 8) return "text-red-600 dark:text-red-400";
  if (score >= 5) return "text-amber-600 dark:text-amber-400";
  return "text-emerald-600 dark:text-emerald-400";
}

export function ImpactScore({ score }: { score: number }) {
  return (
    <span
      className={cn("text-sm font-semibold tabular-nums", getColor(score))}
      title={`Impact score: ${score}/10`}
    >
      {score.toFixed(1)}
    </span>
  );
}
