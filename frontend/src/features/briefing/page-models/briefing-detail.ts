import { getBriefing } from "../api/get-briefing";
import type { BriefingResponseParsed } from "../schemas/briefing";

/**
 * Briefing 詳細 page の view 状態を JSX 非依存で算出する page-model。
 *
 * ADR-005: 詳細は `state` discriminator で ready/empty 分岐を持つため、
 * 切り替えは component 側で `vm.state === "ready"` narrowing する。
 * page-model 自体は identity transform に近いが、将来 keyArticles の article
 * lookup 整形 (articleId → 実 article 配列マージ) 等を入れる時に test
 * 経路が変わらない構造として確立する。
 */
export type BriefingDetailViewModel = BriefingResponseParsed;

export async function getBriefingDetailViewModel(
  slug: string,
): Promise<BriefingDetailViewModel> {
  return getBriefing(slug);
}
