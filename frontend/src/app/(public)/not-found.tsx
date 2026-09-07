import { NotFoundMessage } from "@/components/feedback/NotFoundMessage";
import { PageNavigationReset } from "@/components/layout/PageNavigation";

export default function NotFound() {
  return (
    <>
      <PageNavigationReset />
      <NotFoundMessage message="Page not found." />
    </>
  );
}
