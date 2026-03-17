import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { CategoryBrief } from "@/types";

const colorMap: Record<string, string> = {
  growth_catalyst:
    "bg-blue-100 text-blue-800 border-blue-200 dark:bg-blue-900/30 dark:text-blue-400 dark:border-blue-800",
  risk_mitigation:
    "bg-emerald-100 text-emerald-800 border-emerald-200 dark:bg-emerald-900/30 dark:text-emerald-400 dark:border-emerald-800",
  competitive_edge:
    "bg-purple-100 text-purple-800 border-purple-200 dark:bg-purple-900/30 dark:text-purple-400 dark:border-purple-800",
  regulatory_shift:
    "bg-orange-100 text-orange-800 border-orange-200 dark:bg-orange-900/30 dark:text-orange-400 dark:border-orange-800",
  financial_signal:
    "bg-amber-100 text-amber-800 border-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:border-amber-800",
  market_disruption:
    "bg-rose-100 text-rose-800 border-rose-200 dark:bg-rose-900/30 dark:text-rose-400 dark:border-rose-800",
};

const fallback =
  "bg-gray-100 text-gray-800 border-gray-200 dark:bg-gray-800/30 dark:text-gray-400 dark:border-gray-700";

export function CategoryBadge({ category }: { category: CategoryBrief }) {
  const className = colorMap[category.slug] ?? fallback;
  return (
    <Link href={`/?category=${category.slug}`}>
      <Badge
        variant="outline"
        className={cn("text-xs cursor-pointer hover:opacity-80", className)}
      >
        {category.name}
      </Badge>
    </Link>
  );
}
