"""Shared isolated test resources."""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from fdd_workbench.model.store import open_store


@pytest.fixture
def tmp_duckdb(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a temporary on-disk database and always close its connection."""
    connection = duckdb.connect(str(tmp_path / "test.duckdb"))
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def seeded_store(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a real canonical store populated with the standard taxonomy."""
    connection = open_store(tmp_path / "t.duckdb")
    try:
        yield connection
    finally:
        connection.close()


def seed_entity(
    conn: duckdb.DuckDBPyConnection,
    entity_id: str = "ENT_T",
    name: str = "SIMULATED_ENTITY",
    currency: str = "CNY",
) -> str:
    """Insert one entity and return its ID without creating other context."""
    conn.execute(
        "INSERT INTO entity (entity_id, name, reporting_currency) VALUES (?, ?, ?)",
        [entity_id, name, currency],
    )
    return entity_id


@pytest.fixture
def store_with_entity(seeded_store: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Return a canonical store with one simulated entity and no source files."""
    seed_entity(seeded_store)
    return seeded_store


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    """Write a small three-line CSV under a temporary raw-data directory."""
    path = tmp_path / "raw" / "tb_SIMULATED.csv"
    path.parent.mkdir()
    path.write_text("account,balance\n1001,100\n1122,200\n", encoding="utf-8")
    return path


def seed_minimal_context(conn: duckdb.DuckDBPyConnection) -> tuple[str, str, str]:
    """Insert valid foreign-key context and return entity, period, and file IDs."""
    entity_id = seed_entity(conn, name="Test entity")
    period_id = "ENT_T_2025M12_MGMT_CONS"
    file_id = "F1"
    conn.execute(
        """
        INSERT INTO period (
            period_id, entity_id, start_date, end_date, grain, basis, consolidation_scope
        ) VALUES (?, ?, '2025-12-01', '2025-12-31', 'month', 'management', 'consolidated')
        """,
        [period_id, entity_id],
    )
    conn.execute(
        """
        INSERT INTO source_file (file_id, entity_id, path, sha256, kind)
        VALUES (?, ?, ?, ?, 'tb')
        """,
        [file_id, entity_id, "test_tb.csv", "0" * 64],
    )
    return entity_id, period_id, file_id
