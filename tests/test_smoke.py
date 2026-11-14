"""Packaging smoke test; business checks will be added in later steps."""

from importlib import import_module
from importlib.metadata import version

import duckdb

import fdd_workbench


def test_package_is_importable() -> None:
    """The installed src-layout package is importable without sys.path changes."""
    import_module("fdd_workbench")


def test_package_version_matches_metadata() -> None:
    """The package and installed distribution expose the same version."""
    assert fdd_workbench.__version__ == version("fdd-workbench")


def test_tmp_duckdb_can_query(tmp_duckdb: duckdb.DuckDBPyConnection) -> None:
    """The isolated DuckDB fixture can execute a query."""
    assert tmp_duckdb.execute("SELECT 1").fetchone() == (1,)
