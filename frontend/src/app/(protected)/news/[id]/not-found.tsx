import { NotFoundMessage } from "@/components/feedback/NotFoundMessage";
import { PageNavigationReset } from "@/components/layout/PageNavigation";

export default function NewsNotFound() {
  return (
    <>
      <PageNavigationReset />
      <NotFoundMessage message="Article not found." />
    </>
  );
}
