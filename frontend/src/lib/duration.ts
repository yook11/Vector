/**
 * 経過秒数を "1h 12m" のような短い相対表記に整形する。
 *
 * pipeline status の age 表示専用。`null` は該当イベントが無い欠損を表し "-"
 * を返す。healthy/warning 等の判定は持たず、秒を時・分に分解するだけにとどめる。
 * 運用確認では分の分解能で十分なので秒は丸め、24h を超えても日単位にせず時間で
 * 積む (例: 90000s → "25h")。
 */
export function formatAgeSeconds(seconds: number | null): string {
  if (seconds === null) return "-";
  const totalMinutes = Math.floor(seconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  return `${minutes}m`;
}
