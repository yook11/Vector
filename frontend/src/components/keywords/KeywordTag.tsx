import { Badge } from "@/components/ui/badge";

const categoryColors: Record<string, string> = {
  computing: "bg-blue-100 text-blue-800 border-blue-200",
  materials: "bg-purple-100 text-purple-800 border-purple-200",
  energy: "bg-green-100 text-green-800 border-green-200",
  biotech: "bg-pink-100 text-pink-800 border-pink-200",
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
