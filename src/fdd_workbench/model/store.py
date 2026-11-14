"""Canonical schema initialization and seeded DuckDB connections."""

from importlib import resources
from pathlib import Path

import duckdb

from fdd_workbench.config import STD_ACCOUNTS_CSV
from fdd_workbench.mapping.taxonomy import load_std_accounts, write_std_accounts

SCHEMA_SQL = resources.files("fdd_workbench.model") / "schema.sql"


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Execute the canonical DDL atomically; existing tables are preserved."""
    statements = conn.extract_statements(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.begin()
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def open_store(db_path: Path, taxonomy_csv: Path = STD_ACCOUNTS_CSV) -> duckdb.DuckDBPyConnection:
    """Open a store, initialize its schema, and seed the frozen account taxonomy."""
    conn = duckdb.connect(str(db_path))
    try:
        init_schema(conn)
        write_std_accounts(conn, load_std_accounts(taxonomy_csv))
    except BaseException:
        conn.close()
        raise
    return conn


def list_tables(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Return base-table names from the current database's main schema."""
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_catalog = current_database()
          AND table_schema = 'main'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    ).fetchall()
    return [row[0] for row in rows]
