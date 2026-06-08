/**
 * Briefing feature の Public API。外部 (app/ や他 feature) からはこの index
 * のみを参照する (deep path 禁止、Biome `noRestrictedImports` で構造的に強制)。
 */

export { ArticleCard } from "./components/ArticleCard";
export { BriefingDisclaimer } from "./components/BriefingDisclaimer";
export { BriefingDocument } from "./components/BriefingDocument";
export { BriefingIndexView } from "./components/BriefingIndexView";
export { KeyArticleBlock } from "./components/KeyArticleBlock";
export { WatchPoints } from "./components/WatchPoints";
export {
  type BriefingDetailViewModel,
  getBriefingDetailViewModel,
} from "./page-models/briefing-detail";
export {
  type BriefingListViewModel,
  getBriefingListViewModel,
} from "./page-models/briefing-list";
export type { BriefingArticleSummaryParsed } from "./schemas/briefing";
