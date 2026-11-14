"""Shared isolated test resources."""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest


@pytest.fixture
def tmp_duckdb(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a temporary on-disk database and always close its connection."""
    connection = duckdb.connect(str(tmp_path / "test.duckdb"))
    try:
        yield connection
    finally:
        connection.close()
