import { z } from "zod";

export const LoginSchema = z.object({
  email: z.email(),
  password: z.string().min(1),
});
export type LoginInput = z.infer<typeof LoginSchema>;

export const RegisterSchema = z.object({
  email: z.email(),
  password: z.string().min(8).max(128),
  displayName: z
    .string()
    .trim()
    .max(100)
    .transform((v) => (v.length > 0 ? v : undefined))
    .optional(),
});
export type RegisterInput = z.infer<typeof RegisterSchema>;
