/**
 * paper デザインシステム (紙面様式) の共有 UI 層。
 * news / briefing など複数 feature から参照する design-system 部品 (features 横断)。
 */

export { PaperByline } from "./PaperByline";
export { PaperKicker } from "./PaperKicker";
export { PaperSurface } from "./PaperSurface";
export { PaperTexture } from "./PaperTexture";
export {
  type CategoryKicker,
  formatPaperDate,
  formatPaperMastheadDate,
  formatPaperTime,
  getCategoryKicker,
  getSourceBadge,
  kickerCssVars,
} from "./paper-style";
