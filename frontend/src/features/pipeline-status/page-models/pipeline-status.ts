import type { PipelineHealthResponse } from "@/types/types.gen";
import { getPipelineStatus } from "../api/get-pipeline-status";

/**
 * Pipeline Status page の view 状態を JSX 非依存で算出する page-model。
 *
 * ADR-005 (RSC ユニットテスト戦略) の page-models pattern に準拠。現状は取得結果を
 * そのまま返す identity transform だが、page-model 経路を確立することで将来の
 * display 整形や補助 fetch を加えても test 経路が変わらない構造にする。
 */
export type PipelineStatusViewModel = PipelineHealthResponse;

export async function getPipelineStatusViewModel(): Promise<PipelineStatusViewModel> {
  return getPipelineStatus();
}
