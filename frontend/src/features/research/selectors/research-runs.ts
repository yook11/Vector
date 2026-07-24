import type { ResearchThreadDetail } from "@/types/types.gen";

type ResearchThreadMessage = ResearchThreadDetail["messages"][number];

export function selectActiveResearchRunId(
  messages: readonly ResearchThreadMessage[],
): string | null {
  const active = messages.findLast(
    (message) =>
      message.role === "user" &&
      (message.run.status === "queued" || message.run.status === "running"),
  );
  return active?.role === "user" ? active.run.runId : null;
}

export function selectCommittedResearchRunIds(
  messages: readonly ResearchThreadMessage[],
): string[] {
  return messages.flatMap((message) =>
    message.role === "user" ? [message.run.runId] : [],
  );
}
