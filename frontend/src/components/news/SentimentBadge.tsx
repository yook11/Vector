import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Sentiment } from "@/types";

const config: Record<Sentiment, { label: string; className: string }> = {
  positive: {
    label: "Positive",
    className:
      "bg-emerald-100 text-emerald-800 border-emerald-200 dark:bg-emerald-900/30 dark:text-emerald-400 dark:border-emerald-800",
  },
  negative: {
    label: "Negative",
    className:
      "bg-red-100 text-red-800 border-red-200 dark:bg-red-900/30 dark:text-red-400 dark:border-red-800",
  },
  neutral: {
    label: "Neutral",
    className:
      "bg-gray-100 text-gray-800 border-gray-200 dark:bg-gray-800/30 dark:text-gray-400 dark:border-gray-700",
  },
};

export function SentimentBadge({ sentiment }: { sentiment: Sentiment }) {
  const { label, className } = config[sentiment];
  return (
    <Badge variant="outline" className={cn(className)}>
      {label}
    </Badge>
  );
}
