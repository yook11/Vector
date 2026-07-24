import { NotFoundMessage } from "@/components/feedback/NotFoundMessage";
import { PageNavigationReset } from "@/components/layout/PageNavigation";

// `(protected)` 配下の segment が `notFound()` を呼んだとき、root の
// `app/not-found.tsx` ではなく本 file が render される (Next.js 16 公式の
// route segment fallback)。これにより `(protected)/layout.tsx` の Header が
// 維持され、認証済 user の context が壊れない。
export default function NotFound() {
  return (
    <>
      <PageNavigationReset />
      <NotFoundMessage message="Page not found." />
    </>
  );
}
