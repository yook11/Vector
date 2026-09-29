import type { Pool, PoolClient } from "pg";

export async function deleteAuthUserByEmail(
  pool: Pool,
  email: string,
): Promise<void> {
  const client: PoolClient = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      `DELETE FROM auth.session
       WHERE "userId" IN (SELECT id FROM auth."user" WHERE email = $1)`,
      [email],
    );
    await client.query(
      `DELETE FROM auth.account
       WHERE "userId" IN (SELECT id FROM auth."user" WHERE email = $1)`,
      [email],
    );
    await client.query('DELETE FROM auth."user" WHERE email = $1', [email]);
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}
