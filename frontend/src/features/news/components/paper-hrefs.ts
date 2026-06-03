import type { ArticleQuery } from "@/types";

interface BuildDashboardCategoryHrefInput {
  category?: string;
  pathname?: string;
  query: ArticleQuery;
}

export function buildDashboardCategoryHref({
  category,
  pathname = "/",
  query,
}: BuildDashboardCategoryHrefInput): string {
  const params = new URLSearchParams();

  if (category) params.set("category", category);
  if (query.sortOrder) params.set("sortOrder", query.sortOrder);
  if (query.perPage !== undefined) params.set("perPage", String(query.perPage));

  const qs = params.toString();
  return qs ? `${pathname}?${qs}` : pathname;
}
