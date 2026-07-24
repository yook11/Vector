export const RESEARCH_THREADS = {
  A: {
    id: "00000000-0000-4000-a000-00000000e2a1",
    title: "E2E Research Alpha",
    answer: "Alpha answer marker",
  },
  B: {
    id: "00000000-0000-4000-a000-00000000e2b2",
    title: "E2E Research Beta",
    answer: "Beta answer marker",
  },
  C: {
    id: "00000000-0000-4000-a000-00000000e2c3",
    title: "E2E Research Gamma",
    answer: "Gamma answer marker",
  },
} as const;

export const RESEARCH_CONTINUITY = {
  closed: {
    threadId: "00000000-0000-4000-a000-00000000e2d4",
    activeRunId: "00000000-0000-4000-a000-00000000d4f2",
    title: "E2E Research Continuity Closed",
    completedActiveAnswerMarker:
      "Continuity closed completed active answer marker",
    sourceCount: 14,
    sourceHref: "https://example.com/e2e/research-continuity-closed/source-1",
  },
  open: {
    threadId: "00000000-0000-4000-a000-00000000e2e5",
    activeRunId: "00000000-0000-4000-a000-00000000e5f2",
    title: "E2E Research Continuity Open",
    completedActiveAnswerMarker:
      "Continuity open completed active answer marker",
    sourceCount: 14,
    sourceHref: "https://example.com/e2e/research-continuity-open/source-1",
  },
} as const;

export type ResearchContinuityVariant = keyof typeof RESEARCH_CONTINUITY;
export type ResearchContinuityFixture =
  (typeof RESEARCH_CONTINUITY)[ResearchContinuityVariant];

export const RESEARCH_HISTORY_LIMIT = 20;
export const RESEARCH_EXPANDED_HISTORY_LIMIT =
  RESEARCH_HISTORY_LIMIT + Object.keys(RESEARCH_CONTINUITY).length;
export const RESEARCH_SOURCE_COUNT = 14;
export const RESEARCH_SOURCE_HREF =
  "https://example.com/e2e/research-alpha/source-1";
