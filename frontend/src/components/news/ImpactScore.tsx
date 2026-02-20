import { cn } from "@/lib/utils";

function getColor(score: number): string {
  if (score >= 8) return "text-red-600";
  if (score >= 5) return "text-amber-600";
  return "text-emerald-600";
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
