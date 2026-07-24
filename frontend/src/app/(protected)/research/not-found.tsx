import { NotFoundMessage } from "@/components/feedback/NotFoundMessage";
import { PageNavigationReset } from "@/components/layout/PageNavigation";
import { ResearchRouteRejectedOutcome } from "@/features/research-client";

export default function ResearchNotFound() {
  return (
    <>
      <ResearchRouteRejectedOutcome />
      <PageNavigationReset />
      <NotFoundMessage message="Research thread not found." />
    </>
  );
}
