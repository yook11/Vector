"""接続ロールの実効権限をpublic・authの全オブジェクトから取得する。"""

import asyncpg


async def read_tables(connection: asyncpg.Connection) -> set[tuple[str, str]]:
    rows = await connection.fetch(
        """
        SELECT n.nspname, c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('public', 'auth')
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        """
    )
    return {tuple(row) for row in rows}


async def read_table_columns(
    connection: asyncpg.Connection,
) -> dict[tuple[str, str], set[str]]:
    rows = await connection.fetch(
        """
        SELECT n.nspname, c.relname, a.attname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname IN ('public', 'auth')
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND a.attnum > 0 AND NOT a.attisdropped
        """
    )
    columns: dict[tuple[str, str], set[str]] = {}
    for schema, table, column in rows:
        columns.setdefault((schema, table), set()).add(column)
    return columns


async def read_sequence_owners(
    connection: asyncpg.Connection,
) -> dict[tuple[str, str], str | None]:
    rows = await connection.fetch(
        """
        SELECT n.nspname, s.relname, t.relname AS owner_table
        FROM pg_class s JOIN pg_namespace n ON n.oid = s.relnamespace
        LEFT JOIN pg_depend d
          ON d.classid = 'pg_class'::regclass AND d.objid = s.oid
         AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
        LEFT JOIN pg_class t ON t.oid = d.refobjid
        WHERE s.relkind = 'S' AND n.nspname IN ('public', 'auth')
        """
    )
    return {(schema, sequence): owner_table for schema, sequence, owner_table in rows}


async def read_table_permissions(
    connection: asyncpg.Connection,
) -> set[tuple[str, str, str]]:
    rows = await connection.fetch(
        """
        SELECT n.nspname, c.relname, p.privilege
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY[
            'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
            'REFERENCES', 'TRIGGER', 'MAINTAIN'
        ]) AS operation(name)
        CROSS JOIN LATERAL (
            VALUES (operation.name), (operation.name || ' WITH GRANT OPTION')
        ) AS p(privilege)
        WHERE n.nspname IN ('public', 'auth')
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND has_table_privilege(c.oid, p.privilege)
        """
    )
    return {tuple(row) for row in rows}


async def read_column_permissions(
    connection: asyncpg.Connection,
) -> dict[tuple[str, str, str], set[str]]:
    rows = await connection.fetch(
        """
        SELECT n.nspname, c.relname, p.privilege, a.attname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        CROSS JOIN unnest(ARRAY[
            'SELECT', 'INSERT', 'UPDATE', 'REFERENCES'
        ]) AS operation(name)
        CROSS JOIN LATERAL (
            VALUES (operation.name), (operation.name || ' WITH GRANT OPTION')
        ) AS p(privilege)
        WHERE n.nspname IN ('public', 'auth')
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND a.attnum > 0 AND NOT a.attisdropped
          AND has_column_privilege(c.oid, a.attnum, p.privilege)
        """
    )
    permissions: dict[tuple[str, str, str], set[str]] = {}
    for schema, table, privilege, column in rows:
        permissions.setdefault((schema, table, privilege), set()).add(column)
    return permissions


async def read_sequence_permissions(
    connection: asyncpg.Connection,
) -> set[tuple[str, str, str]]:
    rows = await connection.fetch(
        """
        SELECT n.nspname, c.relname, p.privilege
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['USAGE', 'SELECT', 'UPDATE']) AS operation(name)
        CROSS JOIN LATERAL (
            VALUES (operation.name), (operation.name || ' WITH GRANT OPTION')
        ) AS p(privilege)
        WHERE n.nspname IN ('public', 'auth')
          AND c.relkind = 'S'
          AND has_sequence_privilege(c.oid, p.privilege)
        """
    )
    return {tuple(row) for row in rows}
