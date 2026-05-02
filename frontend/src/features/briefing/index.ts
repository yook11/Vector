/**
 * Briefing feature の Public API。外部 (app/ や他 feature) からはこの index
 * のみを参照する (deep path 禁止、Biome `noRestrictedImports` で構造的に強制)。
 */

export { ArticleCard } from "./components/ArticleCard";
export { BriefingDisclaimer } from "./components/BriefingDisclaimer";
export { BriefingEmptyRow } from "./components/BriefingEmptyRow";
export { BriefingRow } from "./components/BriefingRow";
export { StoryBlock } from "./components/StoryBlock";
export {
  type BriefingDetailViewModel,
  getBriefingDetailViewModel,
} from "./page-models/briefing-detail";
export {
  type BriefingListViewModel,
  getBriefingListViewModel,
} from "./page-models/briefing-list";
