import { z } from "zod";

/**
 * 一覧の続きを指すカーソル (backend が返す不透明な base64url 文字列) の zod schema。
 *
 * Server Action は network 越境なので、受け取った値を形と長さで再検証してから
 * backend と cache key に渡す。上限は backend のカーソル長の上限と揃える。
 */
export const CursorSchema = z
  .string()
  .min(1)
  .max(256)
  .regex(/^[A-Za-z0-9_-]+$/);
