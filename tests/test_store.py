"""Canonical store initialization and database constraint tests."""

from pathlib import Path

import duckdb
import pytest
from tests.conftest import seed_minimal_context

from fdd_workbench.model.store import init_schema, list_tables, open_store


def test_open_store_creates_canonical_tables(seeded_store: duckdb.DuckDBPyConnection) -> None:
    """The complete README schema contains exactly these sixteen tables."""
    expected = {
        "entity",
        "period",
        "source_file",
        "std_account",
        "coa_local",
        "coa_map",
        "reclass_rule",
        "counterparty",
        "trial_balance",
        "gl_entry",
        "statement_line",
        "aging_balance",
        "sales_line",
        "bank_entry",
        "check_run",
        "exception",
    }
    actual = list_tables(seeded_store)
    assert len(actual) == 16
    assert set(actual) == expected


def test_open_store_is_idempotent(tmp_path: Path) -> None:
    """Reinitializing and reopening a stored taxonomy preserves all its rows."""
    db_path = tmp_path / "repeat.duckdb"
    connection = open_store(db_path)
    try:
        before = connection.execute("SELECT * FROM std_account ORDER BY std_code").fetchall()
        assert len(before) == 67
        init_schema(connection)
    finally:
        connection.close()

    reopened = open_store(db_path)
    try:
        after = reopened.execute("SELECT * FROM std_account ORDER BY std_code").fetchall()
        assert after == before
    finally:
        reopened.close()


def test_exception_checks_reject_invalid_values(
    seeded_store: duckdb.DuckDBPyConnection,
) -> None:
    """Invalid impact and severity fail their CHECKs with valid FK context."""
    entity_id, period_id, _ = seed_minimal_context(seeded_store)
    seeded_store.execute(
        """
        INSERT INTO check_run (
            run_id, entity_id, matrix_sha256, target_basis, target_scope, status
        ) VALUES ('RUN_T', ?, ?, 'management', 'consolidated', 'completed')
        """,
        [entity_id, "1" * 64],
    )
    insert_exception = """
        INSERT INTO exception (
            exception_id, run_id, check_id, entity_id, period_id, dimension,
            severity, deal_impact_type, summary, evidence_json
        ) VALUES (?, 'RUN_T', 'INT-TEST', ?, ?, 'integrity', ?, ?, 'Test exception', '[]')
    """
    seeded_store.execute(
        insert_exception,
        ["EX_VALID", entity_id, period_id, "high", "info_only"],
    )
    with pytest.raises(duckdb.ConstraintException, match=r"CHECK.*deal_impact_type"):
        seeded_store.execute(
            insert_exception,
            ["EX_BAD_IMPACT", entity_id, period_id, "high", "commercial_quality"],
        )
    with pytest.raises(duckdb.ConstraintException, match=r"CHECK.*severity"):
        seeded_store.execute(
            insert_exception,
            ["EX_BAD_SEVERITY", entity_id, period_id, "critical", "info_only"],
        )
    assert seeded_store.execute("SELECT count(*) FROM exception").fetchone() == (1,)


def test_trial_balance_rejects_duplicate_grain(
    seeded_store: duckdb.DuckDBPyConnection,
) -> None:
    """Distinct TB IDs cannot bypass the entity/period/local-code unique key."""
    entity_id, period_id, file_id = seed_minimal_context(seeded_store)
    insert_tb = """
        INSERT INTO trial_balance (tb_id, entity_id, period_id, local_code, file_id, row_no)
        VALUES (?, ?, ?, '1122', ?, ?)
    """
    seeded_store.execute(insert_tb, ["TB_1", entity_id, period_id, file_id, 1])
    with pytest.raises(
        duckdb.ConstraintException,
        match=r"Duplicate key.*entity_id.*period_id.*local_code.*unique constraint",
    ):
        seeded_store.execute(insert_tb, ["TB_2", entity_id, period_id, file_id, 2])
    assert seeded_store.execute("SELECT count(*) FROM trial_balance").fetchone() == (1,)
