import { Badge } from "@/components/ui/badge";

const categoryColors: Record<string, string> = {
  computing:
    "bg-blue-100 text-blue-800 border-blue-200 dark:bg-blue-900/30 dark:text-blue-400 dark:border-blue-800",
  materials:
    "bg-purple-100 text-purple-800 border-purple-200 dark:bg-purple-900/30 dark:text-purple-400 dark:border-purple-800",
  energy:
    "bg-green-100 text-green-800 border-green-200 dark:bg-green-900/30 dark:text-green-400 dark:border-green-800",
  biotech:
    "bg-pink-100 text-pink-800 border-pink-200 dark:bg-pink-900/30 dark:text-pink-400 dark:border-pink-800",
};

export function KeywordTag({
  keyword,
  category,
}: {
  keyword: string;
  category: string;
}) {
  const colorClass = categoryColors[category] ?? "";

  return (
    <Badge variant="outline" className={colorClass}>
      {keyword}
    </Badge>
  );
}
