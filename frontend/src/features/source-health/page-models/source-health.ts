import type { SourceHealthResponse } from "@/types/types.gen";
import { getSourceHealth } from "../api/get-source-health";
import { type WindowOption, windowToHours } from "../window";

/**
 * Source Health page の view 状態を JSX 非依存で算出する page-model。
 *
 * ADR-005 (RSC ユニットテスト戦略) の page-models pattern に準拠。window label →
 * windowHours の変換をここで吸収し、page は WindowOption、api は windowHours と
 * 各層の型を分離する。現状は取得結果をそのまま返す identity transform。
 */
export type SourceHealthViewModel = SourceHealthResponse;

export async function getSourceHealthViewModel(
  window: WindowOption,
): Promise<SourceHealthViewModel> {
  return getSourceHealth(windowToHours(window));
}
