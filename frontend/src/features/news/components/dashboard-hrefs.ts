interface BuildDashboardCategoryHrefInput {
  category?: string;
  pathname?: string;
}

export function buildDashboardCategoryHref({
  category,
  pathname = "/",
}: BuildDashboardCategoryHrefInput): string {
  const params = new URLSearchParams();

  if (category) params.set("category", category);

  const qs = params.toString();
  return qs ? `${pathname}?${qs}` : pathname;
}
